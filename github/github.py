from trac.core import *
from trac.config import Option, IntOption, ListOption, BoolOption
from trac.web.api import IRequestFilter, IRequestHandler, Href
from trac.env import IEnvironmentSetupParticipant
from trac.util.translation import _
from hook import CommitHook

import simplejson
import re

from git import Git

class GithubPlugin(Component):
    implements(IRequestHandler, IRequestFilter, IEnvironmentSetupParticipant)


    key           = Option('github', 'apitoken',      '', doc = """Your GitHub API Token found here: https://github.com/account, """)
    closestatus   = Option('github', 'closestatus',   '', doc = """This is the status used to close a ticket. It defaults to closed.""")
    browser       = Option('github', 'browser',       '', doc = """Place your GitHub Source Browser URL here to have the /browser entry point redirect to GitHub.""")
    autofetch     = Option('github', 'autofetch',     '', doc = """Should we auto fetch the repo when we get a commit hook from GitHub.""")
    repo          = Option('trac',   'repository_dir' '', doc = """This is your repository dir""")
    revmap        = Option('github', 'svn_revmap',    '', doc = """a plaintext file mapping svn revisions to git hashes""")
    reread_revmap = Option('github', 'reread_revmap',  0, doc = """force the rereading of the revmap""")

    SCHEMA = Table('svn_revmap', key = ('svn_rev'))[
            Column('svn_rev'),
            Column('git_hash'),
            Index(['svn_rev', 'git_hash']),]


    def __init__(self):
        self.hook = CommitHook(self.env)
        self.env.log.debug("API Token: %s" % self.key)
        self.env.log.debug("Browser: %s" % self.browser)
        self.processHook = False

    # IEnvironmentSetupParticpant methods
    def environment_created(self):
        if revmap:
            self._upgrade_db(self.env.get_db_cnx())

    #return true if the db table doesn't exist or needs to be updated
    def environment_needs_upgrade(self, db):
        if not revmap:
            return False
        if reread_revmap:
            self.config.set('github.reread_revmap', 0)
            self.config.save()
            return True
        cursor = db.cursor()
        try:
            cursor.execute("SELECT COUNT(*) FROM svn_revmap")
            row = cursor.fetchone()
            #if there's one or more rows, assume everything's ok
            if row[0] > 0:
                return False
            return True
        except:
            return True

    def upgrade_environment(self, db):
        self._upgrade_db(db)

    def _upgrade_db(self, db):
        #open the revision map
        try:
            revmap_fd = open(revmap, 'rb')
        except IOError:
            raise ResourceNotFound(_("revision map '%s' not found", revmap)
        cursor = db.cursor()
        try:
            cursor.execute("DROP TABLE svn_revmap;")
        except:
            pass

        db_backend = db.get_db_cnx()
        for stmt in db_backend.to_sql(self.SCHEMA):
            self.env.log(stmt)
            cursor.execute(stmt)

        insert_count = 0
        for line in revmap_fd:
            [svn_rev, git_hash] = line.split()
            insert_query = "INSERT INTO svn_revmap (svn_rev, git_hash) VALUES (%s, %s)" % (svn_rev, git_hash)
            self.env.log.debug(insert_query)
            cursor.execute(insert_query)
            insert_count++

        self.env.log.debug("inserted %d mappings into svn_revmap" % insert_count)

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


    def processChangesetURL(self, req):
        self.env.log.debug("processChangesetURL")
        browser = self.browser.replace('/tree/master', '/commit/')

        url = req.path_info.replace('/changeset/', '')
        self.env.log.debug("url is %s" % url)
        svn_rev_match = re.match( '^([0-9]{1,6})([^0-9a-fA-F]|$)', url)
        if svn_rev_match:
            svn_rev = svn_rev_match.group(1)
            self.env.log.debug("found a svn revision: %s" % svn_rev);
        else:
            self.env.log.debug("didn't find a svn revision");

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


