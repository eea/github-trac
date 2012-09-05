""" Github
"""
# pylint: disable-msg=C0301, C0111

from trac.core import *
from trac.core import Component, implements
from trac.resource import ResourceNotFound
from trac.config import Option
from trac.web.api import IRequestFilter, IRequestHandler, RequestDone
from trac.env import IEnvironmentSetupParticipant
from trac.versioncontrol import RepositoryManager
from trac.util.translation import _
from trac.util.text import shorten_line
from trac.db import Table, Column, Index, DatabaseManager
from trac.wiki import IWikiSyntaxProvider
from genshi.builder import tag
from hook import CommitHook

import re
import os.path
import simplejson

from git import Git

class GithubPlugin(Component):
    implements(IRequestHandler, IRequestFilter, IEnvironmentSetupParticipant,
            IWikiSyntaxProvider)


    key = Option('github', 'apitoken', '', doc="""Your GitHub API Token found here: https://github.com/account, """)
    closestatus = Option('github', 'closestatus', '', doc="""This is the status used to close a ticket. It defaults to closed.""")
    browser = Option('github', 'browser', '', doc="""Place your GitHub Source Browser URL here to have the /browser entry point redirect to GitHub.""")
    autofetch = Option('github', 'autofetch', '', doc="""Should we auto fetch the repo when we get a commit hook from GitHub.""")
    # TODO: Removed following line, obsolete
    # repo = Option('trac', 'repository_dir' '', doc="""This is your repository dir""")
    revmap        = Option('github', 'svn_revmap',    '', doc = """a plaintext file mapping svn revisions to git hashes""")
    enable_revmap = Option('github', 'enable_revmap',  0, doc = """use the svn->git map when a request looks like a svn changeset """)
    long_tooltips = Option('github', 'long_tooltips',  0, doc = """don't shorten tooltips""")

    SCHEMA = [
            Table('svn_revmap', key = ('svn_rev', 'git_hash'))[
                Column('svn_rev', type='int'),
                Column('git_hash'),
                Column('commit_msg'),
                Index(['svn_rev', 'git_hash']),
                ]
            ]


    def __init__(self):
        self.hook = CommitHook(self.env)
        self.env.log.debug("API Token: %s" % self.key)
        self.env.log.debug("Browser: %s" % self.browser)
        self.processHook = False

    # IEnvironmentSetupParticpant methods
    def environment_created(self):
        if int(self.enable_revmap):
            self._upgrade_db(self.env.get_db_cnx())

    #return true if the db table doesn't exist or needs to be updated
    def environment_needs_upgrade(self, db):
        if int(self.enable_revmap) == 0:
            return False
        cursor = db.cursor()
        try:
            cursor.execute("SELECT COUNT(*) FROM svn_revmap")
            row = cursor.fetchone()
            #if there's one or more rows, assume everything's ok
            if row[0] > 0:
                return False
            return True
        except Exception:
            db.rollback()
            return True

    def upgrade_environment(self, db):
        if int(self.enable_revmap):
            self._upgrade_db(db)

    def _upgrade_db(self, db):
        #open the revision map
        if int(self.enable_revmap) == 0:
            return 0
        try:
            revmap_fd = open(self.revmap, 'rb')
        except IOError:
            raise ResourceNotFound(_("revision map '%(revmap)s' not found", revmap=self.revmap))
        cursor = db.cursor()
        try:
            cursor.execute("DROP TABLE svn_revmap;")
        except Exception:
            db.rollback()

        db_backend, _unused = DatabaseManager(self.env)._get_connector()
        cursor = db.cursor()
        for table in self.SCHEMA:
            for stmt in db_backend.to_sql(table):
                self.env.log.debug(stmt)
                cursor.execute(stmt)

        insert_count = 0
        prev_rev = 0
        git_hash = revmap_fd.readline()[0:-1]
        while 1:
            #make sure this line is the hash
            if not re.match(r'[0-9a-f]{40}', git_hash):
                raise Exception("expecting hash, found '%s'" % git_hash)
            line = revmap_fd.readline()[0:-1]

            if line.startswith('git-svn-id:'):
                commit_msg = '<no commit message>'
            else:
                #slurp lines into the commit messsages until there's a blank line, a line starting with git-svn-id or a hash
                commit_msg = ''
                while not re.match(r'[0-9a-f]{40}', line) and not line.startswith('git-svn-id:'):
                    if len(line) > 0:
                        if not commit_msg:
                            commit_msg = line
                        else:
                            commit_msg = commit_msg + " " + line
                    line = revmap_fd.readline()[0:-1]

            if not line.startswith('git-svn-id:'):
                raise Exception("expected git-svn-id, got '%s'" % line)

            svn_rev_match = re.match(r'^git-svn-id:.*@(\d+) ', line)
            svn_rev = int(svn_rev_match.group(1))

            insert_query = "INSERT INTO svn_revmap (svn_rev, git_hash, commit_msg) VALUES (%s, %s, %s);"
            self.env.log.debug(insert_query % (svn_rev, git_hash, commit_msg))
            cursor.execute(insert_query, (svn_rev, git_hash, commit_msg.decode('utf-8')))

            if prev_rev - 1 != svn_rev:
                self.env.log.debug("found a gap between r%d and r%d" % (prev_rev, svn_rev))
            prev_rev = svn_rev

            insert_count += 1
            if svn_rev == 1:
                break
            git_hash = revmap_fd.readline()[0:-1]
            while len(git_hash) == 0:
                git_hash = revmap_fd.readline()[0:-1]

        self.env.log.debug("inserted %d mappings into svn_revmap" % insert_count)

    # IWikiSyntaxProvider methods
    def get_wiki_syntax(self):
        yield (r"\br[1-9]\d*\b",  #svn revision links ("r1432")
            lambda formatter, ns, match:
                self._format_changeset_link(formatter, ns, match))
        yield (r"\b[0-9a-fA-F]{5,40}\b", #git hashes ("eb390eca04394")
            lambda formatter, ns, match:
                self._format_changeset_link(formatter, ns, match))


    #pre_process_request deals with link resolution
    def get_link_resolvers(self):
        return []

    def _format_changeset_link(self, formatter, ns, match):
        self.env.log.debug("format changeset link")
        if int(self.enable_revmap) == 0:
            self.env.log.debug("revmap disabled, skipping thingy")
            return match.group(0)
        self.env.log.debug("revmap enabled: formatting links")
        commit_data = self._get_commit_data(match.group(0))
        if len(commit_data) == 1:
            self.env.log.debug(commit_data)
            if int(self.long_tooltips):
                title = commit_data[0]['msg']
            else:
                title = shorten_line(commit_data[0]['msg'])
            return tag.a(match.group(0), href="%s/%s" % (formatter.href.changeset(), commit_data[0]['id']),
                    title=title, class_="changeset")
        elif len(commit_data) > 1:
            #try to figure out something better when an id is ambiguous
            return match.group(0)

        return match.group(0)

    # IRequestHandler methods
    def match_request(self, req):
        self.env.log.debug("Match Request")
        serve = req.path_info.rstrip('/') == ('/github/%s' % self.key) and req.method == 'POST'
        if serve:
            self.processHook = True
            #This is hacky but it's the only way I found to let Trac post to this request
            #   without a valid form_token
            req.form_token = None

        self.env.log.debug("Handle Request: %s" % serve)
        return serve

    def process_request(self, req):
        if self.processHook:
            self.processCommitHook(req)
        # TODO: Verify this code (and redirect in hook.py also)
        req.send_response(204)
        req.send_header('Content-Length', 0)
        req.write('')
        raise RequestDone

    # This has to be done via the pre_process_request handler
    # Seems that the /browser request doesn't get routed to match_request :(
    def pre_process_request(self, req, handler):
        if self.browser:
            serve = req.path_info.startswith('/browser')
            self.env.log.debug("Handle Pre-Request /browser: %s" % serve)
            if serve:
                self.processBrowserURL(req)

            serve2 = req.path_info.startswith('/changeset')
            try:
                repoinfo = req.path_info.replace('/changeset/', '').partition("/")
            except AttributeError:
                repoinfo = req.path_info.replace('/changeset/', '')
                partition = repoinfo.split('/')
                repoinfo = [partition[0]]
                if len(partition) > 1:
                    repoinfo.append('/')
                    repoinfo.append('/'.join(partition[1:]))
                else:
                    repoinfo.append('')
                    repoinfo.append('')
            repo = self.env.get_repository(repoinfo[2])
            if repo.__class__.__name__ == "GitRepository":
                self.env.log.debug("Handle Pre-Request /changeset: %s" % serve2)
            if serve2:
                self.processChangesetURL(req)

        return handler


    def post_process_request(self, req, template, data, content_type):
        return (template, data, content_type)

    def _get_commit_data(self, commit_id):
        if int(self.enable_revmap) == 0:
            return False
        self.env.log.debug("looking up commit: %s" % commit_id)
        cursor = self.env.get_db_cnx().cursor()
        if commit_id.startswith('r'):
            commit_id = commit_id[1:]
            self.env.log.debug("running query: SELECT git_hash, commit_msg FROM svn_revmap WHERE svn_rev = %s" % commit_id)
            cursor.execute("SELECT git_hash, commit_msg FROM svn_revmap WHERE svn_rev = %s", (commit_id,))
            rows = cursor.fetchmany(5)
        else:
            self.env.log.debug("running query: SELECT git_hash, commit_msg FROM svn_revmap WHERE git_hash LIKE '%s%%'" % commit_id)
            cursor.execute("SELECT git_hash, commit_msg FROM svn_revmap WHERE git_hash LIKE '%s%%'" % (commit_id,))
            rows = cursor.fetchmany(5)
        results = []
        for row in rows:
            #hash is what's in the db, id is the string the user used (usually not the full hash)
            d = {'hash': row[0],
                 'msg' : row[1],
                 'id'  : commit_id,
            }
            results.append(d)
        return results

    def processChangesetURL(self, req):
        self.env.log.debug("processChangesetURL")
        browser = self.browser.replace('/tree/master', '/commit/')

        try:
            commitinfo = req.path_info.replace('/changeset/', '').partition("/")
        except AttributeError:
            commitinfo = req.path_info.replace('/changeset/', '')
            partition = commitinfo.split('/')
            commitinfo = [partition[0]]
            if len(partition) > 1:
                commitinfo.append('/')
                commitinfo.append('/'.join(partition[1:]))
            else:
                commitinfo.append('')
                commitinfo.append('')

        url = "/%s" % (commitinfo[2] + commitinfo[1] + "commit" + commitinfo[1] + commitinfo[0])
        if not url:
            browser = self.browser
            url = ''

        redirect = '%s%s' % (browser, url)
        self.env.log.debug("Redirect URL: %s" % redirect)
        _out = 'Going to GitHub: %s' % redirect

        req.redirect(redirect)


    def processBrowserURL(self, req):
        self.env.log.debug("processBrowserURL")
        rev = req.args.get('rev')
        if rev:
            browser = self.browser.replace('/master', '/')
        else:
            rev = ''
            browser = self.browser

        url = req.path_info.replace('/browser', '')

        redirect = '%s%s%s' % (browser, rev, url)
        self.env.log.debug("Redirect URL: %s" % redirect)
        _out = 'Going to GitHub: %s' % redirect

        req.redirect(redirect)



    def processCommitHook(self, req):
        self.env.log.debug("processCommitHook")
        status = self.closestatus
        if not status:
            status = 'closed'

        if self.autofetch:
            repodir = RepositoryManager(self.env).repository_dir
            if not os.path.isabs(repodir):
                repodir = os.path.join(self.env.path, repodir)
            # TODO: This was the previous code, the repo options is probably unecessary now.
            # repodir = "%s/%s" % (self.repo, reponame)
            self.env.log.debug("Autofetching: %s" % repodir)
            repo = Git(repodir)

            try:
                self.env.log.debug("Fetching repo %s" % self.repo)
                repo.execute(['git', 'fetch'])
                try:
                    self.env.log.debug("Resyncing local repo")
                    self.env.get_repository('').sync()
                except Exception:
                    self.env.log.error("git sync failed!")
            except Exception:
                self.env.log.error("git fetch failed!")

        data = req.args.get('payload')

        if data:
            jsondata = simplejson.loads(data)
            reponame = jsondata['repository']['name']

            for i in jsondata['commits']:
                self.hook.process(i, status, self.enable_revmap, reponame)

        self.env.log.debug("Redirect URL: %s" % req)
        req.redirect(self.browser)
