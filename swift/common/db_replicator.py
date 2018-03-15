# Copyright (c) 2010-2012 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import random
import math
import time
import shutil
import uuid
import errno
import re
from contextlib import contextmanager
from swift import gettext_ as _

from eventlet import GreenPool, sleep, Timeout
from eventlet.green import subprocess

import swift.common.db
from swift.common.constraints import check_drive
from swift.common.direct_client import quote
from swift.common.utils import get_logger, whataremyips, storage_directory, \
    renamer, mkdirs, lock_parent_directory, config_true_value, \
    unlink_older_than, dump_recon_cache, rsync_module_interpolation, \
    json, Timestamp, get_db_files, parse_db_filename
from swift.common import ring
from swift.common.ring.utils import is_local_device
from swift.common.http import HTTP_NOT_FOUND, HTTP_INSUFFICIENT_STORAGE
from swift.common.bufferedhttp import BufferedHTTPConnection
from swift.common.exceptions import DriveNotMounted
from swift.common.daemon import Daemon
from swift.common.swob import Response, HTTPNotFound, HTTPNoContent, \
    HTTPAccepted, HTTPBadRequest


DEBUG_TIMINGS_THRESHOLD = 10


def quarantine_db(object_file, server_type):
    """
    In the case that a corrupt file is found, move it to a quarantined area to
    allow replication to fix it.

    :param object_file: path to corrupt file
    :param server_type: type of file that is corrupt
                        ('container' or 'account')
    """
    object_dir = os.path.dirname(object_file)
    quarantine_dir = os.path.abspath(
        os.path.join(object_dir, '..', '..', '..', '..', 'quarantined',
                     server_type + 's', os.path.basename(object_dir)))
    try:
        renamer(object_dir, quarantine_dir, fsync=False)
    except OSError as e:
        if e.errno not in (errno.EEXIST, errno.ENOTEMPTY):
            raise
        quarantine_dir = "%s-%s" % (quarantine_dir, uuid.uuid4().hex)
        renamer(object_dir, quarantine_dir, fsync=False)


def looks_like_partition(dir_name):
    """
    True if the directory name is a valid partition number, False otherwise.
    """
    try:
        part = int(dir_name)
        return part >= 0
    except ValueError:
        return False


def roundrobin_datadirs(datadirs):
    """
    Generator to walk the data dirs in a round robin manner, evenly
    hitting each device on the system, and yielding any .db files
    found (in their proper places). The partitions within each data
    dir are walked randomly, however.

    :param datadirs: a list of (path, node_id) to walk
    :returns: A generator of (partition, path_to_db_file, node_id)
    """

    def walk_datadir(datadir, node_id):
        partitions = [pd for pd in os.listdir(datadir)
                      if looks_like_partition(pd)]
        random.shuffle(partitions)
        for partition in partitions:
            part_dir = os.path.join(datadir, partition)
            if not os.path.isdir(part_dir):
                continue
            suffixes = os.listdir(part_dir)
            if not suffixes:
                os.rmdir(part_dir)
                continue
            for suffix in suffixes:
                suff_dir = os.path.join(part_dir, suffix)
                if not os.path.isdir(suff_dir):
                    continue
                hashes = os.listdir(suff_dir)
                if not hashes:
                    os.rmdir(suff_dir)
                    continue
                for hsh in hashes:
                    hash_dir = os.path.join(suff_dir, hsh)
                    if not os.path.isdir(hash_dir):
                        continue
                    object_file = os.path.join(hash_dir, hsh + '.db')
                    if os.path.exists(object_file):
                        yield (partition, object_file, node_id)
                        continue
                    # look for any alternate db filenames
                    db_files = get_db_files(object_file)
                    if db_files:
                        yield (partition, db_files[-1], node_id)
                        continue
                    try:
                        os.rmdir(hash_dir)
                    except OSError as e:
                        if e.errno != errno.ENOTEMPTY:
                            raise

    its = [walk_datadir(datadir, node_id) for datadir, node_id in datadirs]
    while its:
        for it in its:
            try:
                yield next(it)
            except StopIteration:
                its.remove(it)


class ReplConnection(BufferedHTTPConnection):
    """
    Helper to simplify REPLICATEing to a remote server.
    """

    def __init__(self, node, partition, hash_, logger):
        self.logger = logger
        self.node = node
        host = "%s:%s" % (node['replication_ip'], node['replication_port'])
        BufferedHTTPConnection.__init__(self, host)
        self.path = '/%s/%s/%s' % (node['device'], partition, hash_)

    def replicate(self, *args):
        """
        Make an HTTP REPLICATE request

        :param args: list of json-encodable objects

        :returns: bufferedhttp response object
        """
        try:
            body = json.dumps(args)
            self.request('REPLICATE', self.path, body,
                         {'Content-Type': 'application/json'})
            response = self.getresponse()
            response.data = response.read()
            return response
        except (Exception, Timeout):
            self.logger.exception(
                _('ERROR reading HTTP response from %s'), self.node)
            return None


class Replicator(Daemon):
    """
    Implements the logic for directing db replication.
    """

    def __init__(self, conf, logger=None):
        self.conf = conf
        self.logger = logger or get_logger(conf, log_route='replicator')
        self.root = conf.get('devices', '/srv/node')
        self.mount_check = config_true_value(conf.get('mount_check', 'true'))
        self.bind_ip = conf.get('bind_ip', '0.0.0.0')
        self.port = int(conf.get('bind_port', self.default_port))
        concurrency = int(conf.get('concurrency', 8))
        self.cpool = GreenPool(size=concurrency)
        swift_dir = conf.get('swift_dir', '/etc/swift')
        self.ring = ring.Ring(swift_dir, ring_name=self.server_type)
        self._local_device_ids = set()
        self.per_diff = int(conf.get('per_diff', 1000))
        self.max_diffs = int(conf.get('max_diffs') or 100)
        self.interval = int(conf.get('interval') or
                            conf.get('run_pause') or 30)
        self.node_timeout = float(conf.get('node_timeout', 10))
        self.conn_timeout = float(conf.get('conn_timeout', 0.5))
        self.rsync_compress = config_true_value(
            conf.get('rsync_compress', 'no'))
        self.rsync_module = conf.get('rsync_module', '').rstrip('/')
        if not self.rsync_module:
            self.rsync_module = '{replication_ip}::%s' % self.server_type
        self.reclaim_age = float(conf.get('reclaim_age', 86400 * 7))
        swift.common.db.DB_PREALLOCATION = \
            config_true_value(conf.get('db_preallocation', 'f'))
        self._zero_stats()
        self.recon_cache_path = conf.get('recon_cache_path',
                                         '/var/cache/swift')
        self.recon_replicator = '%s.recon' % self.server_type
        self.rcache = os.path.join(self.recon_cache_path,
                                   self.recon_replicator)
        self.extract_device_re = re.compile('%s%s([^%s]+)' % (
            self.root, os.path.sep, os.path.sep))

    def _zero_stats(self):
        """Zero out the stats."""
        self.stats = {'attempted': 0, 'success': 0, 'failure': 0, 'ts_repl': 0,
                      'no_change': 0, 'hashmatch': 0, 'rsync': 0, 'diff': 0,
                      'remove': 0, 'empty': 0, 'remote_merge': 0,
                      'start': time.time(), 'diff_capped': 0,
                      'failure_nodes': {}}

    def _report_stats(self):
        """Report the current stats to the logs."""
        now = time.time()
        self.logger.info(
            _('Attempted to replicate %(count)d dbs in %(time).5f seconds '
              '(%(rate).5f/s)'),
            {'count': self.stats['attempted'],
             'time': now - self.stats['start'],
             'rate': self.stats['attempted'] /
                (now - self.stats['start'] + 0.0000001)})
        self.logger.info(_('Removed %(remove)d dbs') % self.stats)
        self.logger.info(_('%(success)s successes, %(failure)s failures')
                         % self.stats)
        dump_recon_cache(
            {'replication_stats': self.stats,
             'replication_time': now - self.stats['start'],
             'replication_last': now},
            self.rcache, self.logger)
        self.logger.info(' '.join(['%s:%s' % item for item in
                         sorted(self.stats.items()) if item[0] in
                         ('no_change', 'hashmatch', 'rsync', 'diff', 'ts_repl',
                          'empty', 'diff_capped', 'remote_merge')]))

    def _add_failure_stats(self, failure_devs_info):
        for node, dev in failure_devs_info:
            self.stats['failure'] += 1
            failure_devs = self.stats['failure_nodes'].setdefault(node, {})
            failure_devs.setdefault(dev, 0)
            failure_devs[dev] += 1

    def _rsync_file(self, db_file, remote_file, whole_file=True,
                    different_region=False):
        """
        Sync a single file using rsync. Used by _rsync_db to handle syncing.

        :param db_file: file to be synced
        :param remote_file: remote location to sync the DB file to
        :param whole-file: if True, uses rsync's --whole-file flag
        :param different_region: if True, the destination node is in a
                                 different region

        :returns: True if the sync was successful, False otherwise
        """
        popen_args = ['rsync', '--quiet', '--no-motd',
                      '--timeout=%s' % int(math.ceil(self.node_timeout)),
                      '--contimeout=%s' % int(math.ceil(self.conn_timeout))]
        if whole_file:
            popen_args.append('--whole-file')

        if self.rsync_compress and different_region:
            # Allow for compression, but only if the remote node is in
            # a different region than the local one.
            popen_args.append('--compress')

        popen_args.extend([db_file, remote_file])
        proc = subprocess.Popen(popen_args)
        proc.communicate()
        if proc.returncode != 0:
            self.logger.error(_('ERROR rsync failed with %(code)s: %(args)s'),
                              {'code': proc.returncode, 'args': popen_args})
        return proc.returncode == 0

    def _rsync_db(self, broker, device, http, local_id,
                  replicate_method='complete_rsync', replicate_timeout=None,
                  different_region=False):
        """
        Sync a whole db using rsync.

        :param broker: DB broker object of DB to be synced
        :param device: device to sync to
        :param http: ReplConnection object
        :param local_id: unique ID of the local database replica
        :param replicate_method: remote operation to perform after rsync
        :param replicate_timeout: timeout to wait in seconds
        :param different_region: if True, the destination node is in a
                                 different region
        """
        rsync_module = rsync_module_interpolation(self.rsync_module, device)
        rsync_path = '%s/tmp/%s' % (device['device'], local_id)
        remote_file = '%s/%s' % (rsync_module, rsync_path)
        mtime = os.path.getmtime(broker.db_file)
        if not self._rsync_file(broker.db_file, remote_file,
                                different_region=different_region):
            return False
        # perform block-level sync if the db was modified during the first sync
        if os.path.exists(broker.db_file + '-journal') or \
                os.path.getmtime(broker.db_file) > mtime:
            # grab a lock so nobody else can modify it
            with broker.lock():
                if not self._rsync_file(broker.db_file, remote_file,
                                        whole_file=False,
                                        different_region=different_region):
                    return False
        with Timeout(replicate_timeout or self.node_timeout):
            response = http.replicate(replicate_method, local_id,
                                      os.path.basename(broker.db_file))
        return response and 200 <= response.status < 300

    def _usync_db(self, point, broker, http, remote_id, local_id,
                  diffs=0):
        """
        Sync a db by sending all records since the last sync.

        :param point: synchronization high water mark between the replicas
        :param broker: database broker object
        :param http: ReplConnection object for the remote server
        :param remote_id: database id for the remote replica
        :param local_id: database id for the local replica
        :param diffs: number of diffs already sent to remote node; the
            maximum number of diffs sent by this method will be reduced by this
            number.

        :returns: a tuple of (boolean indicating completion and success,
            cumulative count of diffs sent)
        """
        self.stats['diff'] += 1
        self.logger.increment('diffs')
        self.logger.debug('%s usyncing chunks to %s, starting at %s',
                          broker.db_file,
                          '%(ip)s:%(port)s/%(device)s' % http.node,
                          point)
        sync_table = broker.get_syncs()
        objects = broker.get_items_since(point, self.per_diff)
        while len(objects) and diffs < self.max_diffs:
            with Timeout(self.node_timeout):
                response = http.replicate('merge_items', objects, local_id)
            if not response or response.status >= 300 or response.status < 200:
                if response:
                    self.logger.error(_('ERROR Bad response %(status)s from '
                                        '%(host)s'),
                                      {'status': response.status,
                                       'host': http.host})
                return False, diffs
            # replication relies on db order to send the next merge batch in
            # order with no gaps
            point = objects[-1]['ROWID']
            objects = broker.get_items_since(point, self.per_diff)
            diffs += 1

        self.logger.debug('%s usyncing chunks to %s, finished at %s',
                          broker.db_file,
                          '%(ip)s:%(port)s/%(device)s' % http.node,
                          point)

        self._sync_other_items(broker, http, local_id)
        if objects:
            self.logger.debug(
                'Synchronization for %s has fallen more than '
                '%s rows behind; moving on and will try again next pass.',
                broker, self.max_diffs * self.per_diff)
            self.stats['diff_capped'] += 1
            self.logger.increment('diff_caps')
        else:
            with Timeout(self.node_timeout):
                response = http.replicate('merge_syncs', sync_table)
            if response and 200 <= response.status < 300:
                broker.merge_syncs([{'remote_id': remote_id,
                                     'sync_point': point}],
                                   incoming=False)
                return True, diffs
        return False, diffs

    def _sync_other_items(self, broker, http, local_id):
        # Attempt to sync other items
        # TODO: the following will have to be cleaned up at some point. The
        # hook here is to grab other items (non-objects) to replicate, namely
        # the shard ranges stored in a container database. The number of these
        # should always be _much_ less than normal objects, however for
        # completeness we should probably have a limit and pointer here too.
        other_items = broker.get_other_replication_items()
        if other_items:
            with Timeout(self.node_timeout):
                response = http.replicate('merge_items', other_items,
                                          local_id)
            if not response or response.status >= 300 \
                    or response.status < 200:
                if response:
                    self.logger.error(_('ERROR Bad response %(status)s '
                                        'from %(host)s'),
                                      {'status': response.status,
                                       'host': http.host})
                # TODO: we probably should pass this back so the replication
                # marked as a failure.
                return False
            self.logger.debug('%s usynced %s other items to %s',
                              broker.db_file, len(other_items),
                              '%(ip)s:%(port)s/%(device)s' % http.node)

    def _other_items_hook(self, broker):
        return []

    def _in_sync(self, rinfo, info, broker, local_sync):
        """
        Determine whether or not two replicas of a databases are considered
        to be in sync.

        :param rinfo: remote database info
        :param info: local database info
        :param broker: database broker object
        :param local_sync: cached last sync point between replicas

        :returns: boolean indicating whether or not the replicas are in sync
        """
        if max(rinfo['point'], local_sync) >= info['max_row']:
            self.stats['no_change'] += 1
            self.logger.increment('no_changes')
            return True
        if rinfo['hash'] == info['hash']:
            self.stats['hashmatch'] += 1
            self.logger.increment('hashmatches')
            broker.merge_syncs([{'remote_id': rinfo['id'],
                                 'sync_point': rinfo['point']}],
                               incoming=False)
            return True

    def _http_connect(self, node, partition, db_file):
        """
        Make an http_connection using ReplConnection

        :param node: node dictionary from the ring
        :param partition: partition partition to send in the url
        :param db_file: DB file

        :returns: ReplConnection object
        """
        hsh, other, ext = parse_db_filename(os.path.basename(db_file))
        return ReplConnection(node, partition, hsh, self.logger)

    def _gather_sync_args(self, info):
        """
        Convert local replication_info to sync args tuple.
        """
        sync_args_order = ('max_row', 'hash', 'id', 'created_at',
                           'put_timestamp', 'delete_timestamp', 'metadata')
        return tuple(info[key] for key in sync_args_order)

    def _repl_db_to_node(self, node, broker, partition, info,
                         different_region=False, diffs=0):
        http = self._http_connect(node, partition, broker.db_file)
        sync_args = self._gather_sync_args(info)
        with Timeout(self.node_timeout):
            response = http.replicate('sync', *sync_args)
        if not response:
            return False, 0
        return self._handle_sync_response(node, response, info, broker, http,
                                          different_region=different_region,
                                          diffs=diffs)

    def _repl_to_node(self, node, broker, partition, info,
                      different_region=False):
        """
        Replicate a database to a node.

        :param node: node dictionary from the ring to be replicated to
        :param broker: DB broker for the DB to be replication
        :param partition: partition on the node to replicate to
        :param info: DB info as a dictionary of {'max_row', 'hash', 'id',
                     'created_at', 'put_timestamp', 'delete_timestamp',
                     'metadata'}
        :param different_region: if True, the destination node is in a
                                 different region

        :returns: True if successful, False otherwise
        """
        diffs = 0
        success = True
        for sub_broker in broker.get_brokers()[:-1]:
            sub_success, diffs = self._repl_db_to_node(
                node, sub_broker, partition, sub_broker.get_replication_info(),
                different_region=different_region, diffs=diffs)
            success &= sub_success
            if not success:
                if diffs:
                    # some but not all of previous broker rows were
                    # successfully sync'd; proceed to subsequent broker, which
                    # may have other items to sync, but prevent it from syncing
                    # any *objects* by setting diffs to max_diffs
                    diffs = self.max_diffs
                else:
                    # state of the destination is uncertain, bail out
                    return False
            # TODO: diffs>0 forces the subsequent broker to use usync up to
            # max_diffs, but if success is True then this broker has
            # successfully sync'd all its rows so we could force diffs to zero
            # and as a consequence allow the subsequent broker to choose any
            # sync method.
            # TODO: if a sub-broker has sync'd with peers then its *outgoing*
            # sync point table should probably be merged into subsequent
            # broker, just like when we first created the shard broker.
        sub_success, _ = self._repl_db_to_node(
            node, broker, partition, info, different_region=different_region,
            diffs=diffs)
        return success & sub_success

    def _handle_sync_response(self, node, response, info, broker, http,
                              different_region=False, diffs=0):
        if response.status == HTTP_NOT_FOUND:  # completely missing, rsync
            self.stats['rsync'] += 1
            self.logger.increment('rsyncs')
            return self._rsync_db(broker, node, http, info['id'],
                                  different_region=different_region), diffs
        elif response.status == HTTP_INSUFFICIENT_STORAGE:
            raise DriveNotMounted()
        elif 200 <= response.status < 300:
            rinfo = json.loads(response.data)
            local_sync = broker.get_sync(rinfo['id'], incoming=False)
            if rinfo.get('metadata', ''):
                broker.update_metadata(json.loads(rinfo['metadata']))
            if self._in_sync(rinfo, info, broker, local_sync):
                self.logger.debug('%s in sync with %s, nothing to do',
                                  broker.db_file,
                                  '%(ip)s:%(port)s/%(device)s' % node)
                return True, diffs
            # if the difference in rowids between the two differs by
            # more than 50% and the difference is greater than per_diff,
            # rsync then do a remote merge.
            # NOTE: difference > per_diff stops us from dropping to rsync
            # on smaller containers, who have only a few rows to sync.
            if (rinfo['max_row'] / float(info['max_row']) < 0.5 and
                    info['max_row'] - rinfo['max_row'] > self.per_diff and
                    not diffs):
                self.stats['remote_merge'] += 1
                self.logger.increment('remote_merges')
                return self._rsync_db(broker, node, http, info['id'],
                                      replicate_method='rsync_then_merge',
                                      replicate_timeout=(info['count'] / 2000),
                                      different_region=different_region), diffs
            # else send diffs over to the remote server
            return self._usync_db(max(rinfo['point'], local_sync),
                                  broker, http, rinfo['id'], info['id'],
                                  diffs=diffs)
        return False, diffs

    def _post_replicate_hook(self, broker, info, responses):
        """
        :param broker: the container that just replicated
        :param info: pre-replication full info dict
        :param responses: a list of bools indicating success from nodes
        """
        pass

    def _replicate_object(self, partition, object_file, node_id):
        """
        Replicate the db, choosing method based on whether or not it
        already exists on peers.

        :param partition: partition to be replicated to
        :param object_file: DB file name to be replicated
        :param node_id: node id of the node to be replicated to
        :returns: a tuple (success, responses). ``success`` is a boolean that
            is True if the method completed successfully, False otherwise.
            ``responses`` is a list of booleans each of which indicates the
            success or not of replicating to a peer node if replication has
            been attempted. ``responses`` may be empty; ``success`` is False
            if any of ``responses`` is False.
        """
        start_time = now = time.time()
        self.logger.debug('Replicating db %s', object_file)
        self.stats['attempted'] += 1
        self.logger.increment('attempts')
        shouldbehere = True
        responses = []
        try:
            broker = self.brokerclass(object_file, pending_timeout=30)
            if self._is_locked(broker):
                # TODO should do something about stats here.
                return
            broker.reclaim(now - self.reclaim_age,
                           now - (self.reclaim_age * 2))
            info = broker.get_replication_info()
            bpart = self.ring.get_part(
                info['account'], info.get('container'))
            if bpart != int(partition):
                partition = bpart
                # Important to set this false here since the later check only
                # checks if it's on the proper device, not partition.
                shouldbehere = False
                name = '/' + quote(info['account'])
                if 'container' in info:
                    name += '/' + quote(info['container'])
                self.logger.error(
                    'Found %s for %s when it should be on partition %s; will '
                    'replicate out and remove.' % (object_file, name, bpart))
        except (Exception, Timeout) as e:
            if 'no such table' in str(e):
                self.logger.error(_('Quarantining DB %s'), object_file)
                quarantine_db(broker.db_file, broker.db_type)
            else:
                self.logger.exception(_('ERROR reading db %s'), object_file)
            nodes = self.ring.get_part_nodes(int(partition))
            self._add_failure_stats([(failure_dev['replication_ip'],
                                      failure_dev['device'])
                                     for failure_dev in nodes])
            self.logger.increment('failures')
            return False, responses
        # The db is considered deleted if the delete_timestamp value is greater
        # than the put_timestamp, and there are no objects.
        delete_timestamp = Timestamp(info.get('delete_timestamp') or 0)
        put_timestamp = Timestamp(info.get('put_timestamp') or 0)
        if (now - self.reclaim_age) > delete_timestamp > put_timestamp and \
                info['count'] in (None, '', 0, '0'):
            if self.report_up_to_date(info):
                self.delete_db(broker)
            self.logger.timing_since('timing', start_time)
            return True, responses
        failure_devs_info = set()
        nodes = self.ring.get_part_nodes(int(partition))
        local_dev = None
        for node in nodes:
            if node['id'] == node_id:
                local_dev = node
                break
        if shouldbehere:
            shouldbehere = bool([n for n in nodes if n['id'] == node_id])
        # See Footnote [1] for an explanation of the repl_nodes assignment.
        if len(nodes) > 1:
            i = 0
            while i < len(nodes) and nodes[i]['id'] != node_id:
                i += 1
            repl_nodes = nodes[i + 1:] + nodes[:i]
        else:  # Special case if using only a single replica
            repl_nodes = nodes
        more_nodes = self.ring.get_more_nodes(int(partition))
        if not local_dev:
            # Check further if local device is a handoff node
            for node in self.ring.get_more_nodes(int(partition)):
                if node['id'] == node_id:
                    local_dev = node
                    break
        for node in repl_nodes:
            different_region = False
            if local_dev and local_dev['region'] != node['region']:
                # This additional information will help later if we
                # want to handle syncing to a node in different
                # region with some optimizations.
                different_region = True
            success = False
            try:
                success = self._repl_to_node(node, broker, partition, info,
                                             different_region)
            except DriveNotMounted:
                try:
                    repl_nodes.append(next(more_nodes))
                except StopIteration:
                    self.logger.error(
                        _('ERROR There are not enough handoff nodes to reach '
                          'replica count for partition %s'),
                        partition)
                self.logger.error(_('ERROR Remote drive not mounted %s'), node)
            except (Exception, Timeout):
                self.logger.exception(_('ERROR syncing %(file)s with node'
                                        ' %(node)s'),
                                      {'file': object_file, 'node': node})
            if not success:
                failure_devs_info.add((node['replication_ip'], node['device']))
            self.logger.increment('successes' if success else 'failures')
            responses.append(success)
        try:
            self._post_replicate_hook(broker, info, responses)
        except (Exception, Timeout):
            self.logger.exception('UNHANDLED EXCEPTION: in post replicate '
                                  'hook for %s', broker.db_file)
        if not shouldbehere and responses and all(responses):
            # If the db shouldn't be on this node and has been successfully
            # synced to all of its peers, it can be removed.
            if not self.delete_db(broker):
                failure_devs_info.update(
                    [(failure_dev['replication_ip'], failure_dev['device'])
                     for failure_dev in repl_nodes])

        target_devs_info = set([(target_dev['replication_ip'],
                                 target_dev['device'])
                                for target_dev in repl_nodes])
        self.stats['success'] += len(target_devs_info - failure_devs_info)
        self._add_failure_stats(failure_devs_info)

        self.logger.timing_since('timing', start_time)
        if shouldbehere:
            responses.append(True)
        return all(responses), responses

    def delete_db(self, broker):
        object_file = broker.db_file
        hash_dir = os.path.dirname(object_file)
        suf_dir = os.path.dirname(hash_dir)
        with lock_parent_directory(object_file):
            shutil.rmtree(hash_dir, True)
        try:
            os.rmdir(suf_dir)
        except OSError as err:
            if err.errno not in (errno.ENOENT, errno.ENOTEMPTY):
                self.logger.exception(
                    _('ERROR while trying to clean up %s') % suf_dir)
                return False
        self.stats['remove'] += 1
        device_name = self.extract_device(object_file)
        self.logger.increment('removes.' + device_name)
        return True

    def extract_device(self, object_file):
        """
        Extract the device name from an object path.  Returns "UNKNOWN" if the
        path could not be extracted successfully for some reason.

        :param object_file: the path to a database file.
        """
        match = self.extract_device_re.match(object_file)
        if match:
            return match.groups()[0]
        return "UNKNOWN"

    def report_up_to_date(self, full_info):
        return True

    def run_once(self, *args, **kwargs):
        """Run a replication pass once."""
        self._zero_stats()
        dirs = []
        ips = whataremyips(self.bind_ip)
        if not ips:
            self.logger.error(_('ERROR Failed to get my own IPs?'))
            return
        self._local_device_ids = set()
        found_local = False
        for node in self.ring.devs:
            if node and is_local_device(ips, self.port,
                                        node['replication_ip'],
                                        node['replication_port']):
                found_local = True
                if not check_drive(self.root, node['device'],
                                   self.mount_check):
                    self._add_failure_stats(
                        [(failure_dev['replication_ip'],
                          failure_dev['device'])
                         for failure_dev in self.ring.devs if failure_dev])
                    self.logger.warning(
                        _('Skipping %(device)s as it is not mounted') % node)
                    continue
                unlink_older_than(
                    os.path.join(self.root, node['device'], 'tmp'),
                    time.time() - self.reclaim_age)
                datadir = os.path.join(self.root, node['device'], self.datadir)
                if os.path.isdir(datadir):
                    self._local_device_ids.add(node['id'])
                    dirs.append((datadir, node['id']))
        if not found_local:
            self.logger.error("Can't find itself %s with port %s in ring "
                              "file, not replicating",
                              ", ".join(ips), self.port)
        self.logger.info(_('Beginning replication run'))
        for part, object_file, node_id in roundrobin_datadirs(dirs):
            self.cpool.spawn_n(
                self._replicate_object, part, object_file, node_id)
        self.cpool.waitall()
        self.logger.info(_('Replication run OVER'))
        self._report_stats()

    def run_forever(self, *args, **kwargs):
        """
        Replicate dbs under the given root in an infinite loop.
        """
        sleep(random.random() * self.interval)
        while True:
            begin = time.time()
            try:
                self.run_once()
            except (Exception, Timeout):
                self.logger.exception(_('ERROR trying to replicate'))
            elapsed = time.time() - begin
            if elapsed < self.interval:
                sleep(self.interval - elapsed)

    def _is_locked(self, broker):
        return False


class ReplicatorRpc(object):
    """Handle Replication RPC calls.  TODO(redbo): document please :)"""

    def __init__(self, root, datadir, broker_class, mount_check=True,
                 logger=None):
        self.root = root
        self.datadir = datadir
        self.broker_class = broker_class
        self.mount_check = mount_check
        self.logger = logger or get_logger({}, log_route='replicator-rpc')

    def _db_file_exists(self, db_path):
        return os.path.exists(db_path)

    def dispatch(self, replicate_args, args):
        if not hasattr(args, 'pop'):
            return HTTPBadRequest(body='Invalid object type')
        op = args.pop(0)
        drive, partition, hsh = replicate_args
        if not check_drive(self.root, drive, self.mount_check):
            return Response(status='507 %s is not mounted' % drive)

        db_file = os.path.join(self.root, drive,
                               storage_directory(self.datadir, partition, hsh),
                               hsh + '.db')
        if op == 'rsync_then_merge':
            return self.rsync_then_merge(drive, db_file, args)
        if op == 'complete_rsync':
            return self.complete_rsync(drive, db_file, args)
        else:
            # someone might be about to rsync a db to us,
            # make sure there's a tmp dir to receive it.
            mkdirs(os.path.join(self.root, drive, 'tmp'))
            if not self._db_file_exists(db_file):
                return HTTPNotFound()
            return getattr(self, op)(self.broker_class(db_file), args)

    @contextmanager
    def debug_timing(self, name):
        timemark = time.time()
        yield
        timespan = time.time() - timemark
        if timespan > DEBUG_TIMINGS_THRESHOLD:
            self.logger.debug(
                'replicator-rpc-sync time for %s: %.02fs' % (
                    name, timespan))

    def _parse_sync_args(self, args):
        """
        Convert remote sync args to remote_info dictionary.
        """
        (remote_sync, hash_, id_, created_at, put_timestamp,
         delete_timestamp, metadata) = args[:7]
        remote_metadata = {}
        if metadata:
            try:
                remote_metadata = json.loads(metadata)
            except ValueError:
                self.logger.error("Unable to decode remote metadata %r",
                                  metadata)
        remote_info = {
            'point': remote_sync,
            'hash': hash_,
            'id': id_,
            'created_at': created_at,
            'put_timestamp': put_timestamp,
            'delete_timestamp': delete_timestamp,
            'metadata': remote_metadata,
        }
        return remote_info

    def sync(self, broker, args):
        remote_info = self._parse_sync_args(args)
        return self._handle_sync_request(broker, remote_info)

    def _get_synced_replication_info(self, broker, remote_info):
        """
        Apply any changes to the broker based on remote_info and return the
        current replication info.

        :param broker: the database broker
        :param remote_info: the remote replication info

        :returns: local broker replication info
        """
        return broker.get_replication_info()

    def _handle_sync_request(self, broker, remote_info):
        """
        Update metadata, timestamps, sync points.
        """
        with self.debug_timing('info'):
            try:
                info = self._get_synced_replication_info(broker, remote_info)
            except (Exception, Timeout) as e:
                if 'no such table' in str(e):
                    self.logger.error(_("Quarantining DB %s"), broker)
                    quarantine_db(broker.db_file, broker.db_type)
                    return HTTPNotFound()
                raise
        if remote_info['metadata']:
            with self.debug_timing('update_metadata'):
                broker.update_metadata(remote_info['metadata'])
        sync_timestamps = ('created_at', 'put_timestamp', 'delete_timestamp')
        if any(info[ts] != remote_info[ts] for ts in sync_timestamps):
            with self.debug_timing('merge_timestamps'):
                broker.merge_timestamps(*(remote_info[ts] for ts in
                                          sync_timestamps))
        with self.debug_timing('get_sync'):
            info['point'] = broker.get_sync(remote_info['id'])
        if remote_info['hash'] == info['hash'] and \
                info['point'] < remote_info['point']:
            with self.debug_timing('merge_syncs'):
                translate = {
                    'remote_id': 'id',
                    'sync_point': 'point',
                }
                data = dict((k, remote_info[v]) for k, v in translate.items())
                broker.merge_syncs([data])
                info['point'] = remote_info['point']
        return Response(json.dumps(info))

    def merge_syncs(self, broker, args):
        broker.merge_syncs(args[0])
        return HTTPAccepted()

    def merge_items(self, broker, args):
        broker.merge_items(args[0], args[1])
        return HTTPAccepted()

    def complete_rsync(self, drive, db_file, args):
        old_filename = os.path.join(self.root, drive, 'tmp', args[0])
        if args[1:]:
            db_file = os.path.join(os.path.dirname(db_file), args[1])
        if os.path.exists(db_file):
            return HTTPNotFound()
        if not os.path.exists(old_filename):
            return HTTPNotFound()
        broker = self.broker_class(old_filename)
        broker.newid(args[0])
        renamer(old_filename, db_file)
        return HTTPNoContent()

    def rsync_then_merge(self, drive, db_file, args):
        old_filename = os.path.join(self.root, drive, 'tmp', args[0])
        if (not self._db_file_exists(db_file) or not
                os.path.exists(old_filename)):
            return HTTPNotFound()
        new_broker = self.broker_class(old_filename)
        existing_broker = self.broker_class(db_file)
        db_file = existing_broker.db_file
        point = -1
        objects = existing_broker.get_items_since(point, 1000)
        while len(objects):
            new_broker.merge_items(objects)
            point = objects[-1]['ROWID']
            objects = existing_broker.get_items_since(point, 1000)
            sleep()
        new_broker.merge_syncs(existing_broker.get_syncs())
        # Note the following hook will need to change to using a pointer and
        # limit in the future.
        other_items = existing_broker.get_other_replication_items()
        if other_items:
            new_broker.merge_items(other_items)
        new_broker.newid(args[0])
        new_broker.update_metadata(existing_broker.metadata)
        renamer(old_filename, db_file)
        return HTTPNoContent()

# Footnote [1]:
#   This orders the nodes so that, given nodes a b c, a will contact b then c,
# b will contact c then a, and c will contact a then b -- in other words, each
# node will always contact the next node in the list first.
#   This helps in the case where databases are all way out of sync, so each
# node is likely to be sending to a different node than it's receiving from,
# rather than two nodes talking to each other, starving out the third.
#   If the third didn't even have a copy and the first two nodes were way out
# of sync, such starvation would mean the third node wouldn't get any copy
# until the first two nodes finally got in sync, which could take a while.
#   This new ordering ensures such starvation doesn't occur, making the data
# more durable.
