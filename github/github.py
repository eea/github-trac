from trac.core import *
from trac.resource import ResourceNotFound
from trac.config import Option, IntOption, ListOption, BoolOption
from trac.web.api import IRequestFilter, IRequestHandler, Href
from trac.env import IEnvironmentSetupParticipant
from trac.util.translation import _
from trac.util.text import shorten_line
from trac.db import Table, Column, Index, DatabaseManager
from trac.wiki import IWikiSyntaxProvider
from genshi.builder import tag
from hook import CommitHook

import simplejson
import re

from git import Git

class GithubPlugin(Component):
    implements(IRequestHandler, IRequestFilter, IEnvironmentSetupParticipant,
            IWikiSyntaxProvider)


    key           = Option('github', 'apitoken',      '', doc = """Your GitHub API Token found here: https://github.com/account, """)
    closestatus   = Option('github', 'closestatus',   '', doc = """This is the status used to close a ticket. It defaults to closed.""")
    browser       = Option('github', 'browser',       '', doc = """Place your GitHub Source Browser URL here to have the /browser entry point redirect to GitHub.""")
    autofetch     = Option('github', 'autofetch',     '', doc = """Should we auto fetch the repo when we get a commit hook from GitHub.""")
    repo          = Option('trac',   'repository_dir' '', doc = """This is your repository dir""")
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
        except:
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
        except:
            db.rollback()

        db_backend, unused = DatabaseManager(self.env)._get_connector()
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
                        if commit_msg:
                            line = commit_msg
                        else:
                            commit_msg = commit_msg + "\n" + line
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
        yield (r"r\d+",  #svn revision links ("r1432")
            lambda formatter, ns, match:
                self._format_changeset_link(formatter, ns, match))
        yield (r"[0-9a-fA-F]{5,40}", #git hashes ("eb390eca04394")
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
        commit_info = self._get_commit_data(match.group(0))
        self.env.log.debug("long tooltips: %s", self.long_tooltips)
        if commit_info:
            if int(self.long_tooltips):
                title = commit_info['msg']
            else:
                title = shorten_line(commit_info['msg'])
            return tag.a(match.group(0), href="%s/%s" % (formatter.href.changeset(), commit_info['id']),
                    title=title, class_="changeset")
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

    # This has to be done via the pre_process_request handler
    # Seems that the /browser request doesn't get routed to match_request :(
    def pre_process_request(self, req, handler):
        if self.browser:
            serve = req.path_info.startswith('/browser')
            self.env.log.debug("Handle Pre-Request /browser: %s" % serve)
            if serve:
                self.processBrowserURL(req)

            serve2 = req.path_info.startswith('/changeset')
            self.env.log.debug("Handle Pre-Request /changeset: %s" % serve2)
            if serve2:
                self.processChangesetURL(req)

        return handler


    def post_process_request(self, req, template, data, content_type):
        return (template, data, content_type)

    def _get_commit_data(self, commit_id):
        if int(self.enable_revmap) == 0:
            return False
        cursor = self.env.get_db_cnx().cursor()
        if commit_id.startswith('r'):
            commit_id = commit_id[1:]
            row = cursor.execute("SELECT git_hash, commit_msg FROM svn_revmap WHERE svn_rev = %s;" % commit_id).fetchone()
            self.env.log.debug("running query: SELECT git_hash, commit_msg FROM svn_revmap WHERE svn_rev = %s;" % commit_id)
        else:
            row = cursor.execute("SELECT git_hash, commit_msg FROM svn_revmap WHERE git_hash LIKE '%s%%';" % commit_id).fetchone()
            self.env.log.debug("running query: SELECT git_hash, commit_msg FROM svn_revmap WHERE git_hash LIKE '%s%%';" % commit_id)
        if row:
            return {
                    'hash': row[0],
                    'msg' : row[1],
                    'id'  : commit_id,
                    }
        return False

    def processChangesetURL(self, req):
        self.env.log.debug("processChangesetURL")
        browser = self.browser.replace('/tree/master', '/commit/')

        url = req.path_info.replace('/changeset/', '')
        self.env.log.debug("url is %s" % url)
        svn_rev_match = re.match( '^([0-9]{1,6})([^0-9a-fA-F]|$)', url)
        if svn_rev_match and int(self.enable_revmap):
            svn_rev = svn_rev_match.group(1)
            commit_data = self._get_commit_data('r'+svn_rev)
            if commit_data:
                url = commit_data['hash']
                self.env.log.debug("mapping svn revision %s to github hash %s" % (svn_rev, commit_data['hash']));
            else:
                self.env.log.debug("couldn't map svn revision %s", svn_rev);
                req.redirect(self.browser)
            #XXX: fail gracefully if it doesn't exist

        if not url:
            browser = self.browser
            url = ''

        redirect = '%s%s' % (browser, url)
        self.env.log.debug("Redirect URL: %s" % redirect)
        out = 'Going to GitHub: %s' % redirect

        req.redirect(redirect)


    def processBrowserURL(self, req):
        self.env.log.debug("processBrowserURL")
        browser = self.browser.replace('/master', '/')
        rev = req.args.get('rev')

        url = req.path_info.replace('/browser', '')
        if not rev:
            rev = ''

        redirect = '%s%s%s' % (browser, rev, url)
        self.env.log.debug("Redirect URL: %s" % redirect)
        out = 'Going to GitHub: %s' % redirect

        req.redirect(redirect)



    def processCommitHook(self, req):
        self.env.log.debug("processCommitHook")
        status = self.closestatus
        if not status:
            status = 'closed'

        data = req.args.get('payload')

        if data:
            jsondata = simplejson.loads(data)

            for i in jsondata['commits']:
                self.hook.process(i, status)

        if self.autofetch:
            repo = Git(self.repo)

            try:
              repo.execute(['git', 'fetch'])
            except:
              self.env.log.debug("git fetch failed!")


