# -*- coding: utf-8 -*-
#
# Copyright (C) 2005-2007 Edgewall Software
# Copyright (C) 2005 Christopher Lenz <cmlenz@gmx.de>
# Copyright (C) 2005-2007 Christian Boos <cboos@neuf.fr>
# All rights reserved.
#
# This software is licensed as described in the file COPYING, which
# you should have received as part of this distribution. The terms
# are also available at http://trac.edgewall.org/wiki/TracLicense.
#
# This software consists of voluntary contributions made by many
# individuals. For the exact contribution history, see the revision
# history and logs, available at http://trac.edgewall.org/log/.
#
# Author: Christopher Lenz <cmlenz@gmx.de>
#         Christian Boos <cboos@neuf.fr>

"""
Note about Unicode:
  All paths (or strings) manipulated by the Subversion bindings are
  assumed to be UTF-8 encoded.

  All paths manipulated by Trac are `unicode` objects.

  Therefore:
   * before being handed out to SVN, the Trac paths have to be encoded to
     UTF-8, using `_to_svn()`
   * before being handed out to Trac, a SVN path has to be decoded from
     UTF-8, using `_from_svn()`

  Warning: `SubversionNode.get_content` returns an object from which one
           can read a stream of bytes.
           NO guarantees can be given about what that stream of bytes
           represents.
           It might be some text, encoded in some way or another.
           SVN properties __might__ give some hints about the content,
           but they actually only reflect the beliefs of whomever set
           those properties...
"""

import os.path
import time
import weakref
import posixpath
from datetime import datetime

from genshi.builder import tag

from trac.config import ListOption
from trac.core import *
from trac.versioncontrol import Changeset, Node, Repository, \
                                IRepositoryConnector, \
                                NoSuchChangeset, NoSuchNode
from trac.versioncontrol.cache import CachedRepository
from trac.versioncontrol.svn_authz import SubversionAuthorizer
from trac.versioncontrol.web_ui.browser import IPropertyRenderer
from trac.util import sorted, embedded_numbers, reversed
from trac.util.text import to_unicode
from trac.util.datefmt import utc

try:
    from svn import fs, repos, core, delta
    has_subversion = True
except ImportError:
    has_subversion = False
    class dummy_svn(object):
        svn_node_dir = 1
        svn_node_file = 2
        def apr_pool_destroy(): pass
        def apr_terminate(): pass
        def apr_pool_clear(): pass
        Editor = object
    delta = core = dummy_svn()
    

_kindmap = {core.svn_node_dir: Node.DIRECTORY,
            core.svn_node_file: Node.FILE}


application_pool = None

def _to_svn(*args):
    """Expect a list of `unicode` path components.
    
    Returns an UTF-8 encoded string suitable for the Subversion python bindings
    (the returned path never starts with a leading "/")
    """
    return '/'.join([p for p in [p.strip('/') for p in args] if p]) \
           .encode('utf-8')

def _from_svn(path):
    """Expect an UTF-8 encoded string and transform it to an `unicode` object
    """
    return path and path.decode('utf-8')
    
def _normalize_path(path):
    """Remove leading "/", except for the root."""
    return path and path.strip('/') or '/'

def _path_within_scope(scope, fullpath):
    """Remove the leading scope from repository paths.

    Return `None` if the path is not is scope.
    """
    if fullpath is not None:
        fullpath = fullpath.lstrip('/')
        if scope == '/':
            return _normalize_path(fullpath)
        scope = scope.strip('/')
        if (fullpath + '/').startswith(scope + '/'):
            return fullpath[len(scope) + 1:] or '/'

def _is_path_within_scope(scope, fullpath):
    """Check whether the given `fullpath` is within the given `scope`"""
    if scope == '/':
        return fullpath is not None
    fullpath = fullpath and fullpath.lstrip('/') or ''
    scope = scope.strip('/')
    return (fullpath + '/').startswith(scope + '/')

# svn_opt_revision_t helpers

def _svn_rev(num):
    value = core.svn_opt_revision_value_t()
    value.number = num
    revision = core.svn_opt_revision_t()
    revision.kind = core.svn_opt_revision_number
    revision.value = value
    return revision

def _svn_head():
    revision = core.svn_opt_revision_t()
    revision.kind = core.svn_opt_revision_head
    return revision

# apr_pool_t helpers

def _mark_weakpool_invalid(weakpool):
    if weakpool():
        weakpool()._mark_invalid()


class Pool(object):
    """A Pythonic memory pool object"""

    # Protect svn.core methods from GC
    apr_pool_destroy = staticmethod(core.apr_pool_destroy)
    apr_terminate = staticmethod(core.apr_terminate)
    apr_pool_clear = staticmethod(core.apr_pool_clear)
    
    def __init__(self, parent_pool=None):
        """Create a new memory pool"""

        global application_pool
        self._parent_pool = parent_pool or application_pool

        # Create pool
        if self._parent_pool:
            self._pool = core.svn_pool_create(self._parent_pool())
        else:
            # If we are an application-level pool,
            # then initialize APR and set this pool
            # to be the application-level pool
            core.apr_initialize()
            application_pool = self

            self._pool = core.svn_pool_create(None)
        self._mark_valid()

    def __call__(self):
        return self._pool

    def valid(self):
        """Check whether this memory pool and its parents
        are still valid"""
        return hasattr(self,"_is_valid")

    def assert_valid(self):
        """Assert that this memory_pool is still valid."""
        assert self.valid();

    def clear(self):
        """Clear embedded memory pool. Invalidate all subpools."""
        self.apr_pool_clear(self._pool)
        self._mark_valid()

    def destroy(self):
        """Destroy embedded memory pool. If you do not destroy
        the memory pool manually, Python will destroy it
        automatically."""

        global application_pool

        self.assert_valid()

        # Destroy pool
        self.apr_pool_destroy(self._pool)

        # Clear application pool and terminate APR if necessary
        if not self._parent_pool:
            application_pool = None
            self.apr_terminate()

        self._mark_invalid()

    def __del__(self):
        """Automatically destroy memory pools, if necessary"""
        if self.valid():
            self.destroy()

    def _mark_valid(self):
        """Mark pool as valid"""
        if self._parent_pool:
            # Refer to self using a weakreference so that we don't
            # create a reference cycle
            weakself = weakref.ref(self)

            # Set up callbacks to mark pool as invalid when parents
            # are destroyed
            self._weakref = weakref.ref(self._parent_pool._is_valid,
                                        lambda x: \
                                        _mark_weakpool_invalid(weakself));

        # mark pool as valid
        self._is_valid = lambda: 1

    def _mark_invalid(self):
        """Mark pool as invalid"""
        if self.valid():
            # Mark invalid
            del self._is_valid

            # Free up memory
            del self._parent_pool
            if hasattr(self, "_weakref"):
                del self._weakref


# Initialize application-level pool
if has_subversion:
    Pool()


class SubversionConnector(Component):

    implements(IRepositoryConnector)

    branches = ListOption('svn', 'branches', 'trunk,branches/*', doc=
        """List of paths categorized as ''branches''.
        If a path ends with '*', then all the directory entries found
        below that path will be included.
        """)

    tags = ListOption('svn', 'tags', 'tags/*', doc=
        """List of paths categorized as ''tags''.
        If a path ends with '*', then all the directory entries found
        below that path will be included.
        """)

    def __init__(self):
        self._version = None

    def get_supported_types(self):
        global has_subversion
        if has_subversion:
            yield ("direct-svnfs", 4)
            yield ("svnfs", 4)
            yield ("svn", 2)

    def get_repository(self, type, dir, authname):
        """Return a `SubversionRepository`.

        The repository is wrapped in a `CachedRepository`.
        """
        if not self._version:
            self._version = self._get_version()
            self.env.systeminfo.append(('Subversion', self._version))
        fs_repos = SubversionRepository(dir, None, self.log,
                                        {'tags': self.tags,
                                         'branches': self.branches})
        if type == 'direct-svnfs':
            repos = fs_repos
        else:
            repos = CachedRepository(self.env.get_db_cnx(), fs_repos, None,
                                     self.log)
        if authname:
            authz = SubversionAuthorizer(self.env, repos, authname)
            repos.authz = fs_repos.authz = authz
        return repos

    def _get_version(self):
        version = (core.SVN_VER_MAJOR, core.SVN_VER_MINOR, core.SVN_VER_MICRO)
        version_string = '%d.%d.%d' % version + core.SVN_VER_TAG
        if version[0] < 1:
            raise TracError("Subversion >= 1.0 required: Found " +
                            version_string)
        return version_string


class SubversionPropertyRenderer(Component):
    implements(IPropertyRenderer)

    def __init__(self):
        self._externals_map = {}

    # IPropertyRenderer methods

    def match_property(self, name, mode):
        return name in ('svn:externals', 'svn:needs-lock') and 4 or 0
    
    def render_property(self, name, mode, context, props):
        if name == 'svn:externals':
            return self._render_externals(props[name])
        elif name == 'svn:needs-lock':
            return self._render_needslock(context)

    def _render_externals(self, prop):
        if not self._externals_map:
            for key, value in self.config.options('svn:externals'):
                # ConfigParser splits at ':', i.e. key='http', value='//...'
                value = value.split()
                key, value = key+':'+value[0], ' '.join(value[1:])
                self._externals_map[key] = value.replace('$path', '%(path)s') \
                                           .replace('$rev', '%(rev)s')
        externals = []
        for external in prop.splitlines():
            elements = external.split()
            if not elements:
                continue
            localpath, rev, url = elements[0], None, elements[-1]
            if len(elements) == 3:
                rev = elements[1]
                rev = rev.replace('-r', '')
            # retrieve a matching entry in the externals map
            prefix = []
            base_url = url
            while base_url:
                if base_url in self._externals_map:
                    break
                base_url, pref = posixpath.split(base_url)
                prefix.append(pref)
            href = self._externals_map.get(base_url)
            revstr = rev and 'at revision '+rev or ''
            if not href and url.startswith('http://'):
                href = url
            if href:
                remotepath = posixpath.join(*reversed(prefix))
                externals.append((localpath, revstr, base_url, remotepath,
                                  href % {'path': remotepath, 'rev': rev}))
            else:
                externals.append((localpath, revstr, url, None, None))
        return tag.ul([tag.li(tag.a(localpath + (not href and ' %s in %s' %
                                                 (rev, url) or ''),
                                    href=href,
                                    title=href and ('%s%s in %s repository' %
                                                    (remotepath, rev, url)) or
                                    'No svn:externals configured in trac.ini'))
                       for localpath, rev, url, remotepath, href in externals])

    def _render_needslock(self, context):
        return tag.img(src=context.href.chrome('common/lock-locked.png'),
                       alt="needs lock", title="needs lock")


class SubversionRepository(Repository):
    """Repository implementation based on the svn.fs API."""

    def __init__(self, path, authz, log, options={}):
        self.log = log
        self.options = options
        self.pool = Pool()
        
        # Remove any trailing slash or else subversion might abort
        if isinstance(path, unicode):
            path = path.encode('utf-8')
        path = os.path.normpath(path).replace('\\', '/')
        self.path = repos.svn_repos_find_root_path(path, self.pool())
        if self.path is None:
            raise TracError("%s does not appear to be a Subversion "
                            "repository." % path)

        self.repos = repos.svn_repos_open(self.path, self.pool())
        self.fs_ptr = repos.svn_repos_fs(self.repos)
        
        uuid = fs.get_uuid(self.fs_ptr, self.pool())
        name = 'svn:%s:%s' % (uuid, path)

        Repository.__init__(self, name, authz, log)

        if self.path != path:
            self.scope = path[len(self.path):]
            if not self.scope[-1] == '/':
                self.scope += '/'
        else:
            self.scope = '/'
        assert self.scope[0] == '/'
        self.clear()

    def clear(self, youngest_rev=None):
        self.youngest = None
        if youngest_rev is not None:
            self.youngest = self.normalize_rev(youngest_rev)
        self.oldest = None

    def __del__(self):
        self.close()

    def has_node(self, path, rev=None, pool=None):
        if not pool:
            pool = self.pool
        rev = self.normalize_rev(rev)
        rev_root = fs.revision_root(self.fs_ptr, rev, pool())
        node_type = fs.check_path(rev_root, _to_svn(self.scope, path), pool())
        return node_type in _kindmap

    def normalize_path(self, path):
        return _normalize_path(path)

    def normalize_rev(self, rev):
        if rev is None or isinstance(rev, basestring) and \
               rev.lower() in ('', 'head', 'latest', 'youngest'):
            return self.youngest_rev
        else:
            try:
                rev = int(rev)
                if rev <= self.youngest_rev:
                    return rev
            except (ValueError, TypeError):
                pass
            raise NoSuchChangeset(rev)

    def close(self):
        self.repos = self.fs_ptr = self.pool = None

    def _get_tags_or_branches(self, paths):
        """Retrieve known branches or tags."""
        for path in self.options.get(paths, []):
            if path.endswith('*'):
                folder = posixpath.dirname(path)
                try:
                    entries = [n for n in self.get_node(folder).get_entries()]
                    for node in sorted(entries, key=lambda n: 
                                       embedded_numbers(n.path.lower())):
                        if node.kind == Node.DIRECTORY:
                            yield node
                except: # no right (TODO: should use a specific Exception here)
                    pass
            else:
                try:
                    yield self.get_node(path)
                except: # no right
                    pass

    def get_quickjump_entries(self, rev):
        """Retrieve known branches, as (name, id) pairs.
        
        Purposedly ignores `rev` and always takes the last revision.
        """
        for n in self._get_tags_or_branches('branches'):
            yield 'branches', n.path, n.path, None
        for n in self._get_tags_or_branches('tags'):
            yield 'tags', n.path, n.created_path, n.created_rev

    def get_changeset(self, rev):
        rev = self.normalize_rev(rev)
        return SubversionChangeset(rev, self.authz, self.scope,
                                   self.fs_ptr, self.pool)

    def get_node(self, path, rev=None):
        path = path or ''
        self.authz.assert_permission(posixpath.join(self.scope,
                                                    path.strip('/')))
        if path and path[-1] == '/':
            path = path[:-1]

        rev = self.normalize_rev(rev) or self.youngest_rev

        return SubversionNode(path, rev, self, self.pool)

    def _history(self, svn_path, start, end, pool):
        """`svn_path` must be a full scope path, UTF-8 encoded string.

        Generator yielding `(path, rev)` pairs, where `path` is an `unicode`
        object.
        Must start with `(path, created rev)`.
        """
        if start < end:
            start, end = end, start
        root = fs.revision_root(self.fs_ptr, start, pool())
        history_ptr = fs.node_history(root, svn_path, pool())
        cross_copies = 1
        while history_ptr:
            history_ptr = fs.history_prev(history_ptr, cross_copies, pool())
            if history_ptr:
                path, rev = fs.history_location(history_ptr, pool())
                if rev < end:
                    break
                path = _from_svn(path)
                if not self.authz.has_permission(path):
                    break
                yield path, rev

    def _previous_rev(self, rev, path='', pool=None):
        if rev > 1: # don't use oldest here, as it's too expensive
            try:
                for _, prev in self._history(_to_svn(self.scope, path),
                                             0, rev-1, pool or self.pool):
                    return prev
            except (SystemError, # "null arg to internal routine" in 1.2.x
                    core.SubversionException): # in 1.3.x
                pass
        return None
    

    def get_oldest_rev(self):
        if self.oldest is None:
            self.oldest = 1
            if self.scope != '/':
                self.oldest = self.next_rev(0, find_initial_rev=True)
        return self.oldest

    def get_youngest_rev(self):
        if not self.youngest:
            self.youngest = fs.youngest_rev(self.fs_ptr, self.pool())
            if self.scope != '/':
                for path, rev in self._history(_to_svn(self.scope),
                                               0, self.youngest, self.pool):
                    self.youngest = rev
                    break
        return self.youngest

    def previous_rev(self, rev, path=''):
        rev = self.normalize_rev(rev)
        return self._previous_rev(rev, path)

    def next_rev(self, rev, path='', find_initial_rev=False):
        rev = self.normalize_rev(rev)
        next = rev + 1
        youngest = self.youngest_rev
        subpool = Pool(self.pool)
        while next <= youngest:
            subpool.clear()            
            try:
                for _, next in self._history(_to_svn(self.scope, path),
                                             rev+1, next, subpool):
                    return next
            except (SystemError, # "null arg to internal routine" in 1.2.x
                    core.SubversionException): # in 1.3.x
                if not find_initial_rev:
                    return next # a 'delete' event is also interesting...
            next += 1
        return None

    def rev_older_than(self, rev1, rev2):
        return self.normalize_rev(rev1) < self.normalize_rev(rev2)

    def get_youngest_rev_in_cache(self, db):
        """Get the latest stored revision by sorting the revision strings
        numerically

        (deprecated, only used for transparent migration to the new caching
        scheme).
        """
        cursor = db.cursor()
        cursor.execute("SELECT rev FROM revision "
                       "ORDER BY -LENGTH(rev), rev DESC LIMIT 1")
        row = cursor.fetchone()
        return row and row[0] or None

    def get_path_history(self, path, rev=None, limit=None):
        path = self.normalize_path(path)
        rev = self.normalize_rev(rev)
        expect_deletion = False
        subpool = Pool(self.pool)
        numrevs = 0
        while rev and (not limit or numrevs < limit):
            subpool.clear()
            if self.has_node(path, rev, subpool):
                if expect_deletion:
                    # it was missing, now it's there again:
                    #  rev+1 must be a delete
                    numrevs += 1
                    yield path, rev+1, Changeset.DELETE
                newer = None # 'newer' is the previously seen history tuple
                older = None # 'older' is the currently examined history tuple
                for p, r in self._history(_to_svn(self.scope, path), 0, rev,
                                          subpool):
                    older = (_path_within_scope(self.scope, p), r,
                             Changeset.ADD)
                    rev = self._previous_rev(r, pool=subpool)
                    if newer:
                        numrevs += 1
                        if older[0] == path:
                            # still on the path: 'newer' was an edit
                            yield newer[0], newer[1], Changeset.EDIT
                        else:
                            # the path changed: 'newer' was a copy
                            rev = self._previous_rev(newer[1], pool=subpool)
                            # restart before the copy op
                            yield newer[0], newer[1], Changeset.COPY
                            older = (older[0], older[1], 'unknown')
                            break
                    newer = older
                if older:
                    # either a real ADD or the source of a COPY
                    numrevs += 1
                    yield older
            else:
                expect_deletion = True
                rev = self._previous_rev(rev, pool=subpool)

    def get_changes(self, old_path, old_rev, new_path, new_rev,
                    ignore_ancestry=0):
        old_node = new_node = None
        old_rev = self.normalize_rev(old_rev)
        new_rev = self.normalize_rev(new_rev)
        if self.has_node(old_path, old_rev):
            old_node = self.get_node(old_path, old_rev)
        else:
            raise NoSuchNode(old_path, old_rev, 'The Base for Diff is invalid')
        if self.has_node(new_path, new_rev):
            new_node = self.get_node(new_path, new_rev)
        else:
            raise NoSuchNode(new_path, new_rev,
                             'The Target for Diff is invalid')
        if new_node.kind != old_node.kind:
            raise TracError('Diff mismatch: Base is a %s (%s in revision %s) '
                            'and Target is a %s (%s in revision %s).' \
                            % (old_node.kind, old_path, old_rev,
                               new_node.kind, new_path, new_rev))
        subpool = Pool(self.pool)
        if new_node.isdir:
            editor = DiffChangeEditor()
            e_ptr, e_baton = delta.make_editor(editor, subpool())
            old_root = fs.revision_root(self.fs_ptr, old_rev, subpool())
            new_root = fs.revision_root(self.fs_ptr, new_rev, subpool())
            def authz_cb(root, path, pool): return 1
            text_deltas = 0 # as this is anyway re-done in Diff.py...
            entry_props = 0 # "... typically used only for working copy updates"
            repos.svn_repos_dir_delta(old_root,
                                      _to_svn(self.scope + old_path), '',
                                      new_root,
                                      _to_svn(self.scope + new_path),
                                      e_ptr, e_baton, authz_cb,
                                      text_deltas,
                                      1, # directory
                                      entry_props,
                                      ignore_ancestry,
                                      subpool())
            for path, kind, change in editor.deltas:
                path = _from_svn(path)
                old_node = new_node = None
                if change != Changeset.ADD:
                    old_node = self.get_node(posixpath.join(old_path, path),
                                             old_rev)
                if change != Changeset.DELETE:
                    new_node = self.get_node(posixpath.join(new_path, path),
                                             new_rev)
                else:
                    kind = _kindmap[fs.check_path(old_root,
                                                  _to_svn(self.scope,
                                                          old_node.path),
                                                  subpool())]
                yield  (old_node, new_node, kind, change)
        else:
            old_root = fs.revision_root(self.fs_ptr, old_rev, subpool())
            new_root = fs.revision_root(self.fs_ptr, new_rev, subpool())
            if fs.contents_changed(old_root, _to_svn(self.scope, old_path),
                                   new_root, _to_svn(self.scope, new_path),
                                   subpool()):
                yield (old_node, new_node, Node.FILE, Changeset.EDIT)


class SubversionNode(Node):

    def __init__(self, path, rev, repos, pool=None):
        self.repos = repos
        self.fs_ptr = repos.fs_ptr
        self.authz = repos.authz
        self.scope = repos.scope
        self._scoped_svn_path = _to_svn(self.scope, path)
        self.pool = Pool(pool)
        self._requested_rev = rev
        pool = self.pool()

        self.root = fs.revision_root(self.fs_ptr, rev, pool)
        node_type = fs.check_path(self.root, self._scoped_svn_path, pool)
        if not node_type in _kindmap:
            raise NoSuchNode(path, rev)
        cr = fs.node_created_rev(self.root, self._scoped_svn_path, pool)
        cp = fs.node_created_path(self.root, self._scoped_svn_path, pool)
        # Note: `cp` differs from `path` if the last change was a copy,
        #        In that case, `path` doesn't even exist at `cr`.
        #        The only guarantees are:
        #          * this node exists at (path,rev)
        #          * the node existed at (created_path,created_rev)
        # Also, `cp` might well be out of the scope of the repository,
        # in this case, we _don't_ use the ''create'' information.
        if _is_path_within_scope(self.scope, cp):
            self.created_rev = cr
            self.created_path = _path_within_scope(self.scope, _from_svn(cp))
        else:
            self.created_rev, self.created_path = rev, path
        self.rev = self.created_rev
        # TODO: check node id
        Node.__init__(self, path, self.rev, _kindmap[node_type])

    def get_content(self):
        if self.isdir:
            return None
        s = core.Stream(fs.file_contents(self.root, self._scoped_svn_path,
                                         self.pool()))
        # Make sure the stream object references the pool to make sure the pool
        # is not destroyed before the stream object.
        s._pool = self.pool
        return s

    def get_entries(self):
        if self.isfile:
            return
        pool = Pool(self.pool)
        entries = fs.dir_entries(self.root, self._scoped_svn_path, pool())
        for item in entries.keys():
            path = posixpath.join(self.path, _from_svn(item))
            if not self.authz.has_permission(posixpath.join(self.scope,
                                                            path.strip('/'))):
                continue
            yield SubversionNode(path, self._requested_rev, self.repos,
                                 self.pool)

    def get_history(self, limit=None):
        newer = None # 'newer' is the previously seen history tuple
        older = None # 'older' is the currently examined history tuple
        pool = Pool(self.pool)
        numrevs = 0
        for path, rev in self.repos._history(self._scoped_svn_path,
                                             0, self._requested_rev, pool):
            path = _path_within_scope(self.scope, path)
            if rev > 0 and path:
                older = (path, rev, Changeset.ADD)
                if newer:
                    if newer[0] == older[0]: # stay on same path
                        change = Changeset.EDIT
                    else:
                        change = Changeset.COPY
                    newer = (newer[0], newer[1], change)
                    numrevs += 1
                    yield newer
                newer = older
            if limit and numrevs >= limit:
                break
        if newer:
            yield newer

    def get_annotations(self):
        annotations = []
        if self.isfile:
            def blame_receiver(line_no, revision, author, date, line, pool):
                annotations.append(revision)
            try:
                rev = _svn_rev(self.rev)
                start = _svn_rev(0)
                repo_url = 'file:///%s/%s' % (self.repos.path.lstrip('/'),
                                              self._scoped_svn_path)
                self.repos.log.info('opening ra_local session to ' + repo_url)
                from svn import client
                client.blame2(repo_url, rev, start, rev, blame_receiver,
                              client.create_context(), self.pool())
            except (core.SubversionException, AttributeError), e:
                # svn thinks file is a binary or blame not supported
                raise TracError('svn blame failed: '+to_unicode(e))
        return annotations

#    def get_previous(self):
#        # FIXME: redo it with fs.node_history

    def get_properties(self):
        props = fs.node_proplist(self.root, self._scoped_svn_path, self.pool())
        for name, value in props.items():
            # Note that property values can be arbitrary binary values
            # so we can't assume they are UTF-8 strings...
            props[_from_svn(name)] = to_unicode(value)
        return props

    def get_content_length(self):
        if self.isdir:
            return None
        return fs.file_length(self.root, self._scoped_svn_path, self.pool())

    def get_content_type(self):
        if self.isdir:
            return None
        return self._get_prop(core.SVN_PROP_MIME_TYPE)

    def get_last_modified(self):
        _date = fs.revision_prop(self.fs_ptr, self.created_rev,
                                 core.SVN_PROP_REVISION_DATE, self.pool())
        if not _date:
            return None
        ts = core.svn_time_from_cstring(_date, self.pool()) / 1000000
        return datetime.fromtimestamp(ts, utc)

    def _get_prop(self, name):
        return fs.node_prop(self.root, self._scoped_svn_path, name,
                            self.pool())


class SubversionChangeset(Changeset):

    def __init__(self, rev, authz, scope, fs_ptr, pool=None):
        self.rev = rev
        self.authz = authz
        self.scope = scope
        self.fs_ptr = fs_ptr
        self.pool = Pool(pool)
        try:
            message = self._get_prop(core.SVN_PROP_REVISION_LOG)
        except core.SubversionException:
            raise NoSuchChangeset(rev)
        author = self._get_prop(core.SVN_PROP_REVISION_AUTHOR)
        # we _hope_ it's UTF-8, but can't be 100% sure (#4321)
        message = message and to_unicode(message, 'utf-8')
        author = author and to_unicode(author, 'utf-8')
        _date = self._get_prop(core.SVN_PROP_REVISION_DATE)
        if _date:
            ts = core.svn_time_from_cstring(_date, self.pool()) / 1000000
            date = datetime.fromtimestamp(ts, utc)
        else:
            date = None
        Changeset.__init__(self, rev, message, author, date)

    def get_properties(self):
        props = fs.revision_proplist(self.fs_ptr, self.rev, self.pool())
        properties = {}
        for k,v in props.iteritems():
            if k not in (core.SVN_PROP_REVISION_LOG,
                         core.SVN_PROP_REVISION_AUTHOR,
                         core.SVN_PROP_REVISION_DATE):
                properties[k] = to_unicode(v)
                # Note: the above `to_unicode` has a small probability
                # to mess-up binary properties, like icons.
        return properties

    def get_changes(self):
        pool = Pool(self.pool)
        tmp = Pool(pool)
        root = fs.revision_root(self.fs_ptr, self.rev, pool())
        editor = repos.RevisionChangeCollector(self.fs_ptr, self.rev, pool())
        e_ptr, e_baton = delta.make_editor(editor, pool())
        repos.svn_repos_replay(root, e_ptr, e_baton, pool())

        idx = 0
        copies, deletions = {}, {}
        changes = []
        revroots = {}
        for path, change in editor.changes.items():
            
            # Filtering on `path`
            if not (_is_path_within_scope(self.scope, path) and \
                    self.authz.has_permission(path)):
                continue

            path = change.path
            base_path = change.base_path
            base_rev = change.base_rev

            # Ensure `base_path` is within the scope
            if not (_is_path_within_scope(self.scope, base_path) and \
                    self.authz.has_permission(base_path)):
                base_path, base_rev = None, -1

            # Determine the action
            if not path:                # deletion
                if base_path:
                    if base_path in deletions:
                        continue # duplicates on base_path are possible (#3778)
                    action = Changeset.DELETE
                    deletions[base_path] = idx
                elif self.scope == '/': # root property change
                    action = Changeset.EDIT
                else:                   # deletion outside of scope, ignore
                    continue
            elif change.added or not base_path: # add or copy
                action = Changeset.ADD
                if base_path and base_rev:
                    action = Changeset.COPY
                    copies[base_path] = idx
            else:
                action = Changeset.EDIT
                # identify the most interesting base_path/base_rev
                # in terms of last changed information (see r2562)
                if revroots.has_key(base_rev):
                    b_root = revroots[base_rev]
                else:
                    b_root = fs.revision_root(self.fs_ptr, base_rev, pool())
                    revroots[base_rev] = b_root
                tmp.clear()
                cbase_path = fs.node_created_path(b_root, base_path, tmp())
                cbase_rev = fs.node_created_rev(b_root, base_path, tmp()) 
                # give up if the created path is outside the scope
                if _is_path_within_scope(self.scope, cbase_path):
                    base_path, base_rev = cbase_path, cbase_rev

            kind = _kindmap[change.item_kind]
            path = _path_within_scope(self.scope, _from_svn(path or base_path))
            base_path = _path_within_scope(self.scope, _from_svn(base_path))
            changes.append([path, kind, action, base_path, base_rev])
            idx += 1

        moves = []
        for k,v in copies.items():
            if k in deletions:
                changes[v][2] = Changeset.MOVE
                moves.append(deletions[k])
        offset = 0
        moves.sort()
        for i in moves:
            del changes[i - offset]
            offset += 1

        changes.sort()
        for change in changes:
            yield tuple(change)

    def _get_prop(self, name):
        return fs.revision_prop(self.fs_ptr, self.rev, name, self.pool())


#
# Delta editor for diffs between arbitrary nodes
#
# Note 1: the 'copyfrom_path' and 'copyfrom_rev' information is not used
#         because 'repos.svn_repos_dir_delta' *doesn't* provide it.
#
# Note 2: the 'dir_baton' is the path of the parent directory
#

class DiffChangeEditor(delta.Editor): 

    def __init__(self):
        self.deltas = []
    
    # -- svn.delta.Editor callbacks

    def open_root(self, base_revision, dir_pool):
        return ('/', Changeset.EDIT)

    def add_directory(self, path, dir_baton, copyfrom_path, copyfrom_rev,
                      dir_pool):
        self.deltas.append((path, Node.DIRECTORY, Changeset.ADD))
        return (path, Changeset.ADD)

    def open_directory(self, path, dir_baton, base_revision, dir_pool):
        return (path, dir_baton[1])

    def change_dir_prop(self, dir_baton, name, value, pool):
        path, change = dir_baton
        if change != Changeset.ADD:
            self.deltas.append((path, Node.DIRECTORY, change))

    def delete_entry(self, path, revision, dir_baton, pool):
        self.deltas.append((path, None, Changeset.DELETE))

    def add_file(self, path, dir_baton, copyfrom_path, copyfrom_revision,
                 dir_pool):
        self.deltas.append((path, Node.FILE, Changeset.ADD))

    def open_file(self, path, dir_baton, dummy_rev, file_pool):
        self.deltas.append((path, Node.FILE, Changeset.EDIT))

