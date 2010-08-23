from trac.core import *
from trac.resource import ResourceNotFound
from trac.config import Option, IntOption, ListOption, BoolOption
from trac.web.api import IRequestFilter, IRequestHandler, Href
from trac.env import IEnvironmentSetupParticipant
from trac.util.translation import _
from trac.db import Table, Column, Index
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

    SCHEMA = Table('svn_revmap', key = ('svn_rev', 'git_hash'))[
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
        if self.enable_revmap:
            self._upgrade_db(self.env.get_db_cnx())

    #return true if the db table doesn't exist or needs to be updated
    def environment_needs_upgrade(self, db):
        if self.enable_revmap == 0:
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
            return True

    def upgrade_environment(self, db):
        if self.enable_revmap:
            self._upgrade_db(db)

    def _upgrade_db(self, db):
        #open the revision map
        try:
            revmap_fd = open(self.revmap, 'rb')
        except IOError:
            raise ResourceNotFound(_("revision map '%(revmap)s' not found", revmap=self.revmap))
        cursor = db.cursor()
        try:
            cursor.execute("DROP TABLE svn_revmap;")
        except:
            pass

        try:
            from trac.db import DatabaseManager
            db_backend, ignored = DatabaseManager(self.env)._get_connector()
        except ImportError:
            db_backend = self.env.get_db_cnx()

        for stmt in db_backend.to_sql(self.SCHEMA):
            self.env.log.debug(stmt)
            cursor.execute(stmt)

        insert_count = 0
        for line in revmap_fd:
            [svn_rev, git_hash] = line.split()
            insert_query = "INSERT INTO svn_revmap (svn_rev, git_hash) VALUES (%s, '%s')" % (svn_rev, git_hash)
            self.env.log.debug(insert_query)
            cursor.execute(insert_query)
            ++insert_count

        self.env.log.debug("inserted %d mappings into svn_revmap" % insert_count)

    # IWikiSyntaxProvider methods
    def get_wiki_syntax(self):
        yield (r"r\d+",  #svn revision links ("r1432")
            lambda formatter, ns, match:
                self._format_changeset_link(formatter, 'svn', ns, match))
        yield (r"[0-9a-fA-F]{5,40}", #git hashes ("eb390eca04394")
            lambda formatter, ns, match:
                self._format_changeset_link(formatter, 'git', ns, match))


    #pre_process_request deals with link resolution
    def get_link_resolvers(self):
        return []

    def _format_changeset_link(self, formatter, rev_type, ns, match):
        git_hash = match.group(0)
        if rev_type == 'svn':
            svn_rev = match.group(0).replace('r','',1)
            git_hash = self._get_git_hash(svn_rev)
        if git_hash:
            return tag.a(match.group(0), href="%s/%s" % (formatter.href.changeset(), git_hash),
                    title="insert title here", class_="changeset")
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

    def _get_git_hash(self, svn_rev):
        cursor = self.env.get_db_cnx().cursor()
        row = cursor.execute("SELECT git_hash FROM svn_revmap WHERE svn_rev = %s;" % svn_rev).fetchone()
        if row:
            return row[0]
        return None

    def processChangesetURL(self, req):
        self.env.log.debug("processChangesetURL")
        browser = self.browser.replace('/tree/master', '/commit/')

        url = req.path_info.replace('/changeset/', '')
        self.env.log.debug("url is %s" % url)
        svn_rev_match = re.match( '^([0-9]{1,6})([^0-9a-fA-F]|$)', url)
        if svn_rev_match and self.enable_revmap:
            svn_rev = svn_rev_match.group(1)
            git_hash = self._get_git_hash(svn_rev)
            if git_hash:
                url = git_hash
                self.env.log.debug("mapping svn revision %s to github hash %s" % (svn_rev, git_hash));
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


