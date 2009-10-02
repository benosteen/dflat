from os import chdir, getcwd, listdir, mkdir, rename, renames, \
               symlink, walk, readlink, remove
from os.path import join as j, abspath, dirname, isdir, isfile, islink
from datetime import datetime
from distutils.dir_util import copy_tree

import re
import urllib
import shutil
import hashlib
import logging
import optparse

def main():
    o = _option_parser()
    values, args = o.parse_args()
    
    cmd = args[0]
    home = _dflat_home(getcwd())
    try:
        version = args[1]
    except IndexError:
        # optional arg not passed
        pass

    if cmd == 'init':
        init(getcwd())
    elif not home:
        print "not a dflat"
    elif cmd == 'checkout':
        checkout(home)
    elif cmd == 'commit':
        commit(home)
    elif cmd == 'status':
        status(home)
    elif cmd == 'export':
        export(home, version)
    else: 
        print "unknown command: %s" % cmd

# decorator for commands to obtain and release lock
def lock(f):
    def new_f(home, *args, **opts):
        _get_lock(home, f)
        result = f(home, *args, **opts)
        _release_lock(home)
        return result
    return new_f

# decorator to log to the dflat home log
def log(f):
    def new_f(home, *args, **opts):
        log_file = j(home, 'log', 'dflat.log')
        _configure_logger(log_file)
        result = f(home, *args, **opts)
        return result
    return new_f

@lock
def init(home):
    contents = filter(lambda x: x != 'lock.txt', listdir(home))
    info = open(j(home, 'dflat-info.txt'), 'w')
    info.write(_anvl('This', 'Dflat/0.10'))
    info.write(_anvl('Manifest-scheme', 'Checkm/0.1'))
    info.write(_anvl('Delta-scheme', 'ReDD/0.1'))
    info.close()
    mkdir(j(home, 'log'))
    version = _new_version(home)
    _set_current(home, version)
    # move original inhabitants into their new apartment
    for f in contents:
        rename(j(home, f), j(home, version, 'full', 'data', f))
    _update_manifest(j(home, version))

    # can't use decorator since the log directory doesn't exist when 
    # init is called
    log_file = j(home, 'log', 'dflat.log')
    _configure_logger(log_file)
    logging.info("initialized dflat: %s" % home)

@log
@lock
def checkout(home):
    v1 = _current_version(home)
    v2 = _next_version(home)
    if isdir(j(home, v2)):
        print "%s already checked out" % v2
        return v2
    shutil.copytree(j(home, v1), j(home, v2))
    logging.info('checked out new version %s' % v2)
    print "checked out %s" % v2
    return v2 

@log
@lock
def commit(home, msg=None):
    v1 = _current_version(home)
    v2 = _latest_version(home)
    if v1 == v2:
        print "nothing to commit"
        return
    _update_manifest(j(home, v2))
    delta = _delta(home, v1, v2)
    if not _has_changes(delta):
        print "no changes"
        return 

    redd_home = j(home, v1, 'redd')
    mkdir(redd_home)
    open(j(redd_home, '0=redd_0.1'), 'w').write('redd 0.1')

    if len(delta['deleted']) > 0:
        mkdir(j(redd_home, 'add'))
        for filename in delta['deleted']:
            renames(j(home, v1, 'full', filename), j(redd_home, 'add', filename))
    if len(delta['added']) > 0:
        delete = open(j(redd_home, 'delete.txt'), 'w')
        for filename in delta['added']:
            delete.write("%s\n" % filename)
        delete.close()
    if len(delta['modified']) > 0:
        if not isdir(j(redd_home, 'add')):
            mkdir(j(redd_home, 'add'))
        delete = open(j(redd_home, 'delete.txt'), 'a')
        for filename in delta['modified']:
            delete.write("%s\n" % filename)
            renames(j(home, v1, 'full', filename), j(redd_home, 'add', filename))
        delete.close()
    shutil.rmtree(j(home, v1, 'full'))
    remove(j(home, 'current'))
    _set_current(home, v2)
    logging.info('committed %s %s' % (v2, delta))
    print "committed %s" % v2
    return v2

@log
def export(home, version):
    # validate specified version
    versions = _versions(home)
    if version not in versions:
        raise Exception("version %s not found in %s" % (version, ", ".join(versions)))
    # copy the latest version
    current_version = _current_version(home)
    export = 'export-%s' % version
    shutil.copytree(j(home, current_version), j(home, export))
    # walk back from latest version-1 to specified version, applying changes
    delta_versions = _versions(home,
                               reverse=True,
                               from_version=current_version,
                               to_version=version)[1:] 
    # apply adds, deletes, and replaces
    for dv in delta_versions:
        # delete deleted files
        if isfile(j(home, dv, 'redd', 'delete.txt')):
            deletes = open(j(home, dv, 'redd', 'delete.txt')).read().split()
            for delete in deletes:
                remove(j(home, export, 'full', delete))
        # add added files
        if isdir(j(home, dv, 'redd', 'add')): 
            for f in listdir(j(home, dv, 'redd', 'add')):
                copy_tree(j(home, dv, 'redd', 'add', f), j(home, export, 'full', f)) 
    logging.info('exported version %s' % version)

def status(home):
    print "dflat home: %s" % home
    v1 = _current_version(home)
    print "current version: %s" % v1
    v2 = _latest_version(home)
    if v1 == v2:
        print "no changes"
        delta = None
    else:
        _update_manifest(j(home, v2))
        delta = _delta(home, v1, v2)
        _print_delta_files(delta, 'added')
        _print_delta_files(delta, 'modified')
        _print_delta_files(delta, 'deleted')
    return delta

def _update_manifest(version_dir): 
    full_dir = j(version_dir, 'full')
    manifest_file = j(full_dir, 'manifest.txt')
    manifest = open(manifest_file, 'w')
    for dirpath, dirnames, filenames in walk(full_dir):
        for filename in filenames:
            if dirpath != 'full' and filename in ('manifest.txt', 'lock.txt'):
                continue
            # make the filename relative to the 'full' directory
            dirpath = re.sub(r'^%s/?' % full_dir, '', dirpath)
            md5 = _md5(j(full_dir, dirpath, filename))
            filename = urllib.quote(j(dirpath, filename))
            manifest.write("%s md5 %s\n" % (filename, md5))
    manifest.close()
    return manifest_file

def _current_version(home):
    if islink(j(home, 'current')):
        return readlink(j(home, 'current'))
    else:
        return None

def _anvl(name, value):
    return "%s: %s\n"

def _get_lock(home, caller):
    # TODO: log this operation?
    lockfile = j(home, 'lock.txt')
    if isfile(lockfile):
        raise Exception("already locked")
    # TODO: change this to use w3c date format
    d = datetime.now().isoformat()
    agent = "dflat-%s" % caller.func_name
    lockfile = open(lockfile, 'w')
    lockfile.write("Lock: %s %s\n" % (d, agent))
    lockfile.close()

def _release_lock(home):
    # TODO: log this operation?
    lockfile = j(home, 'lock.txt')
    if not isfile(lockfile):
        return
    remove(lockfile)

def _new_version(home):
    v = _next_version(home)
    mkdir(j(home, v))
    mkdir(j(home, v, 'full'))
    mkdir(j(home, v, 'full', 'admin'))
    mkdir(j(home, v, 'full', 'annotation'))
    mkdir(j(home, v, 'full', 'data'))
    mkdir(j(home, v, 'full', 'enrichment'))
    open(j(home, v, 'full', 'manifest.txt'), 'w')
    open(j(home, v, 'full', 'relationships.ttl'), 'w')
    open(j(home, v, 'full', 'splash.txt'), 'w')
    return v

def _next_version(home):
    v = _current_version(home)
    if v == None:
        return 'v001'
    else:
        return 'v%03i' % (_version_number(v) + 1)

def _latest_version(home):
    versions = _versions(home)
    if len(versions) == 0:
        return None
    else:
        return versions.pop()

def _versions(home, reverse=False, from_version=None, to_version=None):
    versions = filter(lambda x: re.match('^v\d+$', x), listdir(home))
    if from_version:
        versions = [x for x in versions if _version_number(x) <= _version_number(from_version)]
    if to_version:
        versions = [x for x in versions if _version_number(x) >= _version_number(to_version)]
    versions.sort(lambda a, b: cmp(_version_number(a), _version_number(b)))
    if reverse:
        versions.sort(lambda a, b: cmp(_version_number(b), _version_number(a)))
    return versions

def _version_number(version_dir):
    return int(version_dir[1:])

def _md5(filename):
    f = open(filename, 'rb')
    m = hashlib.md5()
    while True:
        bytes = f.read(0x1000)
        if not bytes:
            break
        m.update(bytes)
    f.close()
    return m.hexdigest()

def _delta(home, v1, v2):
    delta = {'modified': [], 'deleted': [], 'added': []}
    manifest_v1 = _manifest_dict(home, v1)
    manifest_v2 = _manifest_dict(home, v2)
    for filename in manifest_v2.keys():
        if manifest_v1.has_key(filename):
            if manifest_v2[filename] != manifest_v1[filename]:
                delta['modified'].append(filename)
        else:
            delta['added'].append(filename)
    for filename in manifest_v1.keys():
        if not manifest_v2.has_key(filename):
            delta['deleted'].append(filename)
    return delta

def _print_delta_files(delta, dtype):
    files = delta[dtype]
    files.sort()
    if len(files) > 0:
        print "%s:" % dtype
        for filename in files:
            print "  %s" % urllib.unquote(filename)

def _has_changes(delta):
    for v in delta.values():
        if len(v) > 0:
            return True
    return False

def _manifest_dict(home, v):
    d = {}
    for line in open(j(home, v, 'full', 'manifest.txt')):
        if line.startswith('#'):
            continue
        cols = line.split()
        d[urllib.unquote(cols[0])] = cols[2]
    return d

def _dflat_home(directory):
    if 'dflat-info.txt' in listdir(directory):
        return abspath(directory)
    elif directory == '/':
        return None
    else:
        return _dflat_home(abspath(dirname(directory)))

def _option_parser():
    parser = optparse.OptionParser()
    return parser

def _set_current(home, v):
    # chdir to make symlink relative, so the dflat can be relocated
    # maybe there's a more elegant way to do this?
    pwd = getcwd()
    chdir(home)
    if isfile('current'):
        remove('current')
    symlink(v, 'current')
    chdir(pwd)

def _configure_logger(filename):
    logging.basicConfig(filename=filename, 
                        level=logging.INFO, 
                        format='%(asctime)s %(levelname)-8s %(message)s',
                        datefmt='%Y-%m-%dT%H:%M:%S')
