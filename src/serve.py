import os
import sys
import bz2
import json
import traceback
import re
import collections
import urllib
import time
import difflib

from cStringIO import StringIO
from optparse import OptionParser
from flask import Flask, render_template, make_response, request, Response
import trac
import patchbot
import db

from db import tickets
from util import (now_str, current_reports, latest_version,
                  compare_version, parse_datetime)

IMAGES_DIR = '/home/patchbot/sage-patchbot/src/images/'


def timed_cached_function(refresh_rate=60):

    def decorator(func):
        cache = {}

        def wrap(*args):
            now = time.time()
            if args in cache:
                latest_update, res = cache[args]
                if now - latest_update < refresh_rate:
                    return res
            res = func(*args)
            cache[args] = now, res
            return res
        return wrap
    return decorator


@timed_cached_function()
def latest_base(betas=False):
    versions = list(db.tickets.find({'id': 0}).distinct('reports.base'))
    if not betas:
        versions = filter(re.compile(r'[0-9.]+$').match, versions)
    versions.sort(compare_version)
    return versions[-1]

app = Flask(__name__)


@app.route("/reports")
def reports():
    pass


def compute_trusted_authors():
    """
    Define the trusted authors.

    Currently, somebody is trusted if he/she is the author of a closed patch
    with 'fixed' status.

    Then 'git' and 'vbraun_spam' are added.

    The result is a dict, its keys being the trusted authors.

    This needs work ! We cannot rely on the branch names!
    """
    authors = collections.defaultdict(int)
    authors['git'] += 1
    authors['vbraun_spam'] += 1
    for ticket in tickets.find({'status': 'closed : fixed'}):
        for author in ticket["authors"]:
            authors[author] += 1
    for ticket in tickets.find({'status': 'closed', 'resolution': 'fixed'}):
        for author in ticket["authors"]:
            authors[author] += 1
        for author in ticket.get("authors_fullnames", []):
            authors[author] += 1
    return authors


@app.route("/trusted")
@app.route("/trusted/")
def trusted_authors():
    """
    Serve a web page with the set of trusted authors.

    Either as json dict or in human-readable format.

    See http://patchbot.sagemath.org/trusted/

    and http://patchbot.sagemath.org/trusted/?pretty

    The dict of trusted authors is computed in ``compute_trusted_authors``.
    """
    authors = compute_trusted_authors()
    if 'pretty' in request.args:
        indent = 4
    else:
        indent = None
    response = make_response(json.dumps(authors, default=lambda x: None,
                                        indent=indent))
    response.headers['Content-type'] = 'text/plain'
    return response


@app.route("/trust_check")
def trust_check():
    """
    Serve a web page that tells if some given authors are trusted.

    This is at destination of human readers.

    The question must be asked as follows:

    trust_check?who=balzac,zola
    """
    authors = compute_trusted_authors()
    given_list = request.args['who'].split(',')
    trust_dict = {a: 'trusted' if a in authors else 'not trusted'
                  for a in given_list}
    response = make_response(json.dumps(trust_dict, default=lambda x: None,
                                        indent=4))
    response.headers['Content-type'] = 'text/plain'
    return response


def get_query(args):
    """
    Prepare the precise query for the database.

    The result is a dict.

    get_query({'participant':'yop'})
    """
    if 'query' in args:
        query = json.loads(args.get('query'))
    else:
        status = args.get('status', 'needs_review')
        if status == 'all':
            query = {}
        elif status in ('new', 'closed'):
            query = {'status': {'$regex': status + '.*'}}
        elif status in ('open',):
            query = {'status': {'$regex': 'needs_.*|positive_review'}}
        else:
            query = {'status': status}

        if 'authors' in args:
            query['authors'] = {'$in': args.get('authors')}
        if 'machine' in args:
            query['reports.machine'] = args['machine'].split(':')
        if 'ticket' in args:
            query['id'] = int(args['ticket'])

    if 'author' in args:
        query['authors'] = args.get('author')
    if 'participant' in args:
        query['participants'] = args.get('participant')
    print query
    return query


@app.route("/")
@app.route("/ticket")
@app.route("/ticket/")
def ticket_list():
    authors = None
    machine = None
    query = get_query(request.args)
    if 'machine' in request.args:
        machine = request.args.get('machine').split(':')
    if 'authors' in request.args:
        authors = request.args.get('authors').split(':')
    if 'order' in request.args:
        order = request.args.get('order')
    else:
        order = 'last_activity'
    limit = int(request.args.get('limit', 10000))
    print query
    if 'base' in request.args:
        base = request.args.get('base')
        if base == 'all':
            base = None
    else:
        base = 'latest'
    all = patchbot.filter_on_authors(tickets.find(query).sort(order).limit(limit), authors)
    if 'raw' in request.args:
        def filter_reports(all):
            for ticket in all:
                current = sorted(current_reports(ticket),
                                 key=lambda report: parse_datetime(report['time']))
                ticket['reports'] = list(reversed(current))[:10]
                for report in ticket['reports']:
                    report['plugins'] = '...'
                yield ticket
        all = filter_reports(all)
        if 'pretty' in request.args:
            indent = 4
        else:
            indent = None
        response = make_response(json.dumps(list(all), default=lambda x: None,
                                            indent=indent))
        response.headers['Content-type'] = 'text/plain'
        return response
    summary = {key: 0 for key in status_order}

    def preprocess(all):
        for ticket in all:
            ticket['report_count'], ticket['report_status'], ticket['report_status_composite'] = get_ticket_status(ticket, machine=machine, base=base or 'latest')
            if 'reports' in ticket:
                ticket['pending'] = len([r for r in ticket['reports']
                                         if r['status'] == 'Pending'])
            summary[ticket['report_status']] += 1
            yield ticket
    ticket0 = db.lookup_ticket(0)
    versions = list(set(report['base'] for report in ticket0['reports']))
    versions.sort(compare_version)
    versions = [(v, get_ticket_status(ticket0, v)) for v in versions]
    return render_template("ticket_list.html", tickets=preprocess(all), summary=summary, base=base, base_status=get_ticket_status(db.lookup_ticket(0), base), versions=versions, status_order=status_order, compare_version=compare_version)


class MachineStats:
    def __init__(self, name):
        self.name = name
        self.fresh_tickets = set()
        self.all_tickets = set()
        self.report_count = 0
        self.last_report = ''

    def add_report(self, report, ticket):
        self.report_count += 1
        self.all_tickets.add(ticket['id'])
        if report.get('git_commit') == ticket.get('git_commit'):
            self.fresh_tickets.add(ticket['id'])
        self.last_report = max(report['time'], self.last_report)

    def __cmp__(self, other):
        return cmp(self.last_report, other.last_report)


@app.route("/machines")
def machines():
    """
    list of recently working machines, with some statistics
    """
    # aggregate requires server version >= 2.1.0
    query = get_query(request.args)
    if 'authors' in request.args:
        authors = request.args.get('authors').split(':')
    else:
        authors = None
    all = patchbot.filter_on_authors(tickets.find(query).limit(100), authors)
    machines = {}
    for ticket in all:
        for report in ticket.get('reports', []):
            machine = tuple(report['machine'])
            if machine in machines:
                stats = machines[machine]
            else:
                stats = machines[machine] = MachineStats(machine)
            stats.add_report(report, ticket)
    all = []
    return render_template("machines.html",
                           machines=reversed(sorted(machines.values())),
                           len=len,
                           status=request.args.get('status', 'needs_review'))


def format_patches(ticket, patches, deps=None, required=None):
    if deps is None:
        deps = []
    if required is not None:
        required = set(required)

    def format_item(item):
        if required is None or item in required:
            note = ""
        else:
            note = "<span style='color: red'>(mismatch)</span>"
        item = str(item)
        if '#' in item:
            url = trac.get_patch_url(ticket, item, raw=False)
            title = item
        elif '.' in item:
            url = '/?base=%s' % item
            title = 'sage-%s' % item
        else:
            url = '/ticket/%s' % item
            title = '#%s' % item
        return "<a href='%s'>%s</a> %s" % (url, title, note)

    missing_deps = missing_patches = ''
    if required is not None:
        required_patches_count = len([p for p in required if '#' in str(p)])
        if len(deps) < len(required) - required_patches_count:
            missing_deps = "<li><span style='color: red'>(missing deps)</span>\n"
        if len(patches) < required_patches_count:
            missing_patches = "<li><span style='color: red'>(missing patches)</span>\n"
    if not missing_deps and not deps and not patches and not missing_patches:
        return ''
    return ("<ol>"
            + missing_deps
            + "<li>\n"
            + "\n<li>".join(format_item(patch) for patch in (deps + patches))
            + missing_patches
            + "</ol>")


@app.route("/ticket/<int:ticket>/")
def render_ticket(ticket):
    """
    reports on a given ticket

    possible options: ?force and ?kick
    """
    try:
        info = trac.scrape(ticket, db=db, force='force' in request.args)
    except:
        info = tickets.find_one({'id': ticket})
    if info is None:
        return "No such ticket."
    if 'kick' in request.args:
        info['retry'] = True
        db.save_ticket(info)
    if 'reports' in info:
        info['reports'].sort(lambda a, b: -cmp(a['time'], b['time']))
    else:
        info['reports'] = []

    base_reports = base_reports_by_machine_and_base()

    old_reports = list(info['reports'])
    patchbot.prune_pending(info)
    if old_reports != info['reports']:
        db.save_ticket(info)

    def format_info(info):
        new_info = {}
        for key, value in info.items():
            if key == 'patches':
                # new_info['patches'] = format_patches(ticket, value)
                pass
            elif key == 'reports' or key == 'pending':
                pass
            elif key == 'depends_on':
                deps_status = {}

                def is_int(a):
                    try:
                        int(a)
                        return True
                    except ValueError:
                        return False
                for dep in tickets.find({'id': {'$in': [int(a) for a in value
                                                        if is_int(a)]}},
                                        ['status', 'id']):
                    if 'closed' in dep['status']:
                        dep['style'] = 'text-decoration: line-through'
                    else:
                        dep['style'] = ''
                    deps_status[dep['id']] = dep
                new_info[key] = ', '.join("<img src='/ticket/%s/status.png?fast' height=16><a href='/ticket/%s' style='%s'>%s</a>" % (a, a, deps_status[a]['style'], a) for a in value)
            elif key == 'authors':
                new_info[key] = ', '.join("<a href='/ticket/?author=%s'>%s</a>" % (a, a) for a in value)
            elif key == 'participants':
                parts = ', '.join("<a href='/ticket/?participant=%s'>%s</a>" % (a, a) for a in value)
                trust_check = "(<a href='/trust_check?who="
                trust_check += ','.join("%s" % a for a in value)
                trust_check += "'>Check trust</a>) "
                new_info[key] = trust_check + parts
            elif isinstance(value, list):
                new_info[key] = ', '.join(value)
            elif key not in ('id', '_id'):
                new_info[key] = value
        return new_info
    base = latest_base()

    def format_git_describe(res):
        if res:
            if '-' in res:
                tag, commits = res.split('-')[:2]
                return "%s + %s commits" % (tag, commits)
            elif 'commits' in res:
                # old style
                return res
            else:
                return res + " + 0 commits"
        else:
            return '?'

    def preprocess_reports(all):
        for item in all:
            base_report = base_reports.get(item['base'] + "/" + "/".join(item['machine']), base_reports.get(item['base']))
            if base_report:
                item['base_log'] = urllib.quote(log_name(0, base_report))
            if 'patches' in item:
                pass
                # required = info['depends_on'] + info['patches']
                # item['patch_list'] = format_patches(ticket, item['patches'], item.get('deps'), required)
            if 'git_base' in item:
                git_log = item.get('git_log')
                item['git_log_len'] = '?' if git_log is None else len(git_log)
            item['raw_base'] = item['base']
            if compare_version(item['base'], base) < 0:
                item['base'] = "<span style='color: red'>%s</span>" % item['base']
            if 'time' in item:
                item['log'] = log_name(info['id'], item)
            if 'git_commit_human' not in item:
                item['git_commit_human'] = "%s new commits" % len(item['log'])
            for x in ('commit', 'base', 'merge'):
                field = 'git_%s_human' % x
                item[field] = format_git_describe(item.get(field, None))
            yield item

    def normalize_plugin(plugin):
        while len(plugin) < 3:
            plugin.append(None)
        return plugin

    def sort_fields(items):
        return sorted(items, key=(lambda x: (x[0] != 'title', x)))

    return render_template("ticket.html",
                           reports=preprocess_reports(info['reports']),
                           ticket=ticket, info=format_info(info),
                           status=get_ticket_status(info, base=base)[2],
                           normalize_plugin=normalize_plugin,
                           sort_fields=sort_fields)


@timed_cached_function(10)
def base_reports_by_machine_and_base():
    """
    reports on the base branch (pseudo-ticket 0)
    """
    return reports_by_machine_and_base(tickets.find_one({'id': 0}))


def reports_by_machine_and_base(ticket):
    """
    reports on the given ticket
    """
    all = {}
    if 'reports' in ticket:
        # oldest to newest
        for report in sorted(ticket['reports'],
                             lambda a, b: cmp(a['time'], b['time'])):
            all[report['base']] = report
            all[report['base'] + "/" + "/".join(report['machine'])] = report
    return all

# The fact that this image is in the trac template lets the patchbot
# know when a page gets updated.


@app.route("/ticket/<int:ticket>/status.png")
def render_ticket_status(ticket):
    """
    Return the status image for the given ticket.
    """
    try:
        if 'fast' in request.args:
            info = tickets.find_one({'id': ticket})
        else:
            info = trac.scrape(ticket, db=db)
    except:
        info = tickets.find_one({'id': ticket})

    if 'base' in request.args:
        base = request.args.get('base')
    else:
        base = latest_version(info.get('reports', []))

    status = get_ticket_status(info, base=base)[2]  # composite status

    if 'fast' in request.args:
        display_base = None
    else:
        display_base = base

    response = make_response(create_status_image(status, base=display_base))
    response.headers['Content-type'] = 'image/png'
    response.headers['Cache-Control'] = 'no-cache'
    return response


@app.route("/ticket/<int:ticket>/status.svg")
def render_ticket_status_svg(ticket):
    """
    Return the svg status image for the given ticket.
    """
    try:
        if 'fast' in request.args:
            info = tickets.find_one({'id': ticket})
        else:
            info = trac.scrape(ticket, db=db)
    except:
        info = tickets.find_one({'id': ticket})

    if 'base' in request.args:
        base = request.args.get('base')
    else:
        base = latest_version(info.get('reports', []))

    status = get_ticket_status(info, base=base)[1]  # single status

    path = status_image_path(status, type='svg')
    response = make_response(open(path).read())
    response.headers['Content-type'] = 'image/svg+xml'
    response.headers['Cache-Control'] = 'no-cache'
    return response


@app.route("/report/<int:ticket_id>", methods=['POST'])
def post_report(ticket_id):
    """
    Posting a report to the database of reports.
    """
    try:
        ticket = db.lookup_ticket(ticket_id)
        if ticket is None:
            ticket = trac.scrape(ticket_id)
        if 'reports' not in ticket:
            ticket['reports'] = []
        report = json.loads(request.form.get('report'))
        assert (isinstance(report, dict)), "report is not a dict"
        for fld in ['status', 'spkgs', 'base', 'machine', 'time']:
            assert (fld in report), "{} missing in report".format(fld)
        patchbot.prune_pending(ticket, report['machine'])
        ticket['reports'].append(report)
        db.logs.put(request.files.get('log'), _id=log_name(ticket_id, report))
        if 'retry' in ticket:
            ticket['retry'] = False
        ticket['last_activity'] = now_str()
        db.save_ticket(ticket)
        return "ok (report successfully posted)"
    except:
        traceback.print_exc()
        return "error in posting the report"


def log_name(ticket_id, report):
    return "/log%s/%s/%s/%s" % (
        '/Pending' if report['status'] == 'Pending' else '',
        ticket_id,
        '/'.join(report['machine']),
        report['time'])


def shorten(lines):
    """
    Extract a shorter log from the full log by removing boring parts
    """
    timing = re.compile(r'\s*\[(\d+ tests?, )?\d+\.\d* s\]\s*$')
    skip = re.compile(r'(sage -t.*\(skipping\))|(byte-compiling)|(copying)|(\S+: \d+% \(\d+ of \d+\)|(Build finished. The built documents can be found in.*)|(\[.........\] .*)|(cp.*/mac-app/.*)|(creating.*site-packages/sage.*)|(mkdir.*)|(creating build/.*)|(Deleting empty directory.*)|(;;;.*))$')
    gcc = re.compile('(gcc)|(g\+\+)')
    prev = None
    in_plugin = False
    from patchbot import plugin_boundary
    plugin_start = re.compile(plugin_boundary('.*'))
    plugin_end = re.compile(plugin_boundary('.*', end=True))
    for line in StringIO(lines):
        if line.startswith('='):
            if plugin_end.match(line):
                if prev:
                    yield prev
                    prev = None
                in_plugin = False
            elif plugin_start.match(line):
                if prev:
                    yield prev
                    prev = None
                yield line
                in_plugin = True
        if in_plugin:
            prev = line
            continue

        if skip.match(line):
            pass
        elif prev is None:
            prev = line
        elif prev.startswith('sage -t') and timing.match(line):
            prev = None
        elif prev.startswith('python `which cython`') and '-->' in line:
            prev = None
        elif gcc.match(prev) and (gcc.match(line) or
                                  line.startswith('Time to execute')):
            prev = line
        else:
            if prev is not None:
                yield prev
            prev = line

    if prev is not None:
        yield prev


def extract_plugin_log(data, plugin):
    """
    Extract from data the log of a given plugin.
    """
    from patchbot import plugin_boundary
    start = plugin_boundary(plugin) + "\n"
    end = plugin_boundary(plugin, end=True) + "\n"
    all = []
    include = False
    for line in StringIO(data):
        if line == start:
            include = True
        if include:
            all.append(line)
        if line == end:
            break
    return ''.join(all)


@app.route("/ticket/<id>/log/<path:log>")
def get_ticket_log(id, log):
    return get_log(log)


@app.route("/log/<path:log>")
def get_log(log):
    path = "/log/" + log
    if not db.logs.exists(path):
        data = "No such log!"
    else:
        data = bz2.decompress(db.logs.get(path).read())
    if 'plugin' in request.args:
        plugin = request.args.get('plugin')
        data = extract_plugin_log(data, plugin)
        if 'diff' in request.args:
            header = data[:data.find('\n')]
            base = request.args.get('base')
            ticket_id = request.args.get('ticket')
            base_data = bz2.decompress(db.logs.get(request.args.get('diff')).read())
            base_data = extract_plugin_log(base_data, plugin)
            diff = difflib.unified_diff(base_data.split('\n'), data.split('\n'), base, "%s + #%s" % (base, ticket_id), n=0)
            data = data = '\n'.join(('' if item[0] == '@' else item)
                                    for item in diff)
            if not data:
                data = "No change."
            data = header + "\n\n" + data

    if 'short' in request.args:
        response = Response(shorten(data), direct_passthrough=True)
    else:
        response = make_response(data)
    response.headers['Content-type'] = 'text/plain; charset=utf-8'
    return response


@app.route("/ticket/<id>/plugin/<plugin_name>/<timestamp>/")
def get_plugin_data(id, plugin_name, timestamp):
    ticket = tickets.find_one({'id': int(id)})
    if ticket is None:
        return "Unknown ticket: " + id
    for report in ticket['reports']:
        if report['time'] == timestamp:
            for plugin in report['plugins']:
                if plugin[0] == plugin_name:
                    response = make_response(json.dumps(plugin[2],
                                                        default=lambda x: None,
                                                        indent=4))
                    response.headers['Content-type'] = 'text/plain'
                    return response
            return "Unknown plugin: " + plugin_name
    return "Unknown report: " + timestamp


status_order = ['New', 'ApplyFailed', 'BuildFailed', 'TestsFailed',
                'PluginFailed', 'TestsPassed', 'Pending',
                'PluginOnlyFailed', 'PluginOnly', 'NoPatch', 'Spkg']


status_colors = {'New': 'white',
                 'ApplyFailed': 'red',
                 'BuildFailed': 'orange',
                 'TestsFailed': 'yellow',
                 'TestsPassed': 'green',
                 'PluginFailed': 'blue',
                 'Pending': 'white',
                 'PluginOnly': 'lightgreen',
                 'PluginOnlyFailed': 'lightblue',
                 'NoPatch': 'purple',
                 'Spkg': 'purple'}


@app.route("/blob/<status>")
def status_image(status):
    """
    Return the blob image (as a web page) for a single status or a
    concatenation of several ones

    This is for the 'png' icon set.

    For example, see http://patchbot.sagemath.org/blob/BuildFailed,ApplyFailed

    or http://patchbot.sagemath.org/blob/TestsPassed
    """
    response = make_response(create_status_image(status))
    response.headers['Content-type'] = 'image/png'
    response.headers['Cache-Control'] = 'max-age=3600'
    return response


@app.route("/svg/<status>")
def status_image_svg(status):
    """
    Return the blob image (as a web page) for a single status

    This is for the 'svg' icon set.

    For example, see http://patchbot.sagemath.org/blob/BuildFailed

    or http://patchbot.sagemath.org/blob_svg/TestsPassed
    """
    liste = status.split(',')
    # Only one possible status displayed. Which one to choose ?
    # stupid choice for the moment
    if len(liste) > 1:
        status = liste[0]
    path = status_image_path(status, type='svg')
    response = make_response(open(path).read())
    response.headers['Content-type'] = 'image/svg+xml'
    response.headers['Cache-Control'] = 'max-age=3600'
    return response


def status_image_path(status, type='png'):
    """
    Return the blob image address for a single status

    There are two different icon sets : 'png' and 'svg'

    For example, the result for 'TestsPassed' should be images/green-blob.png
    """
    if type == 'png':
        return IMAGES_DIR + '{}-blob.png'.format(status_colors[status])
    else:
        return IMAGES_DIR + 'icon-{}.svg'.format(status)


def create_status_image(status, base=None):
    """
    Return a composite blob image for a concatenation of status

    This is for the 'png' icon set.

    INPUT:

    - status -- a single or composite status as a single string

    - base -- the base

    EXAMPLES::

        create_status_image('TestsPassed,TestsFailed')
        create_status_image('NoPatch')
    """
    if ',' in status:
        status_list = status.split(',')
        # Ignore plugin only...
        while 'PluginOnly' in status_list and len(status_list) > 1:
            status_list.remove('PluginOnly')
        # If tests passed but a plugin-only failed, report as if the
        # plugin failed.
        if 'TestsPassed' in status_list:
            for ix, status in enumerate(status_list):
                if status_list[ix] == 'PluginOnlyFailed':
                    status_list[ix] = 'PluginFailed'
        if len(status_list) == 0:
            path = status_image_path('New')
        elif len(set(status_list)) == 1:
            status = status_list[0]
            path = status_image_path(status)
        else:
            try:
                from PIL import Image
                import numpy
                if not os.path.exists(IMAGES_DIR + '_cache'):
                    os.mkdir(IMAGES_DIR + '_cache')
                path = IMAGES_DIR + '_cache/' + ','.join(status_list) + '-blob.png'
                if not os.path.exists(path):
                    composite = numpy.asarray(Image.open(status_image_path(status_list[0]))).copy()
                    height, width, _ = composite.shape
                    for ix, status in enumerate(reversed(status_list)):
                        slice = numpy.asarray(Image.open(status_image_path(status)))
                        start = ix * width / len(status_list)
                        end = (ix + 1) * width / len(status_list)
                        composite[:, start:end, :] = slice[:, start:end, :]
                    Image.fromarray(composite, 'RGBA').save(path)
            except ImportError, exn:
                print exn
                status = min_status(status_list)
                path = status_image_path(status)
    else:
        path = status_image_path(status)
    if base is not None:
        try:
            from PIL import Image, ImageDraw
            im = Image.open(path)
            ImageDraw.Draw(im).text((5, 20), base.replace("alpha", "a").replace("beta", "b"), fill='#FFFFFF')
            output = StringIO()
            im.save(output, format='png')
            return output.getvalue()
        except ImportError:
            return open(path).read()
    else:
        return open(path).read()


def min_status(status_list):
    """
    Return the minimal status among a list of status.

    The order is deduced from a total order encoded in ``status_order``.

    EXAMPLES::

        >>> min_status(['TestsPassed', 'TestsFailed'])
    """
    index = min(status_order.index(status) for status in status_list)
    return status_order[index]


@app.route("/robots.txt")
def robots():
    """
    Return a robot instruction web page

    See http://patchbot.sagemath.org/robots.txt for the result.

    EXAMPLES::

        sage: from serve import robots
        sage: robots()
        ?
    """
    return render_template("robots.txt")


@app.route("/favicon.ico")
def favicon():
    """
    Return the favicon image as a web page (green blob)

    See http://patchbot.sagemath.org/favicon.ico for the result.

    EXAMPLES::

        sage: from serve import favicon
        sage: favicon()
    """
    response = make_response(open(IMAGES_DIR + 'icon-TestsPassed.png').read())
    response.headers['Content-type'] = 'image/png'
    return response


def get_ticket_status(ticket, base=None, machine=None):
    """
    Return the status of the ticket in the database.

    INPUT:

    - ``ticket`` -- dictionary

    - ``base`` -- keyword passed to ``current_reports``

    - ``machine`` -- if given, only look at this machine's reports

    OUTPUT:

    a triple (number of reports, single status, composite status)

    Note that ``Spkg``, ``NoPatch`` and ``New`` are not got from any report.
    """
    all = current_reports(ticket, base=base)
    if machine is not None:
        all = [r for r in all if r['machine'] == machine]
    if all:
        status_list = [report['status'] for report in all]
        if len(set(status_list)) == 1:
            composite = single = status_list[0]
        else:
            composite = ','.join(status_list)
            single = min_status(status_list)
        return len(all), single, composite
    elif ticket['spkgs']:
        return 0, 'Spkg', 'Spkg'
    elif not ticket.get('git_commit'):
        return 0, 'NoPatch', 'NoPatch'
    else:
        return 0, 'New', 'New'


def main(args):
    parser = OptionParser()
    parser.add_option("-p", "--port", dest="port")
    parser.add_option("--debug", dest="debug", default=False)
    (options, args) = parser.parse_args(args)

    app.run(debug=options.debug, host="0.0.0.0", port=int(options.port))

if __name__ == '__main__':
    main(sys.argv)
