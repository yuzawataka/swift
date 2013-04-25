# Copyright (c) 2010-2013 OpenStack, LLC.
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
from multiprocessing import Process, Queue
from os.path import basename, isdir, join
from pyinotify import ProcessEvent, WatchManager, Notifier, \
    IN_DELETE, IN_CREATE, IN_MODIFY, IN_ATTRIB, IN_Q_OVERFLOW, IN_UNMOUNT
from eventlet import sleep
from time import time


class NotifyProc(ProcessEvent):
    """
    pyinotufy process event class
    called by ObjectReplicator().partitions_notifier()
    """
    def my_init(self, **kargs):
        self.parts_queue = kargs['parts_queue']
        self.paths = kargs['paths']
        self.wm = kargs['watch_manager']
        self.add_watch_part = kargs['add_watch_part']
        self.mask = kargs['mask']
        self.logger = kargs['logger']
        self.queue_timeout = 5

    def process_IN_CREATE(self, event):
        if self._pre_path_check(event.pathname):
            return
        if self.logger:
            self.logger.debug('%s: %s' % (event.maskname, event.pathname))
        part = self.part_num(event.pathname)
        if self.add_watch_part:
            self.wm.add_watch(event.pathname,
                              self.mask, rec=True,
                              auto_add=True, do_glob=False,
                              exclude_filter=NotifyExcludeFilter())
        if self.logger:
            self.logger.debug('inotifier watching %s directory.' % len(self.wm.watches))
        self.parts_queue.put(self.part_num(event.pathname), True, self.queue_timeout)
        return

    def process_IN_DELETE(self, event):
        if self._pre_path_check(event.pathname):
            return
        if self.logger:
            self.logger.debug('%s: %s' % (event.maskname, event.pathname))
        part = self.part_num(event.pathname)
        if event.pathname.endswith(part):
            if self.add_watch_part:
                for key, val in self.wm.watches.items():
                    if val.path.startswith(event.pathname):
                        self.vm.rm_watch(val.wd)
            if self.logger:
                self.logger.debug('inotifier watching %s directory.' % len(self.wm.watches))
            return
        self.parts_queue.put(part, True, self.queue_timeout)
        if self.logger:
            self.logger.debug('inotifier watching %s directory.' % len(self.wm.watches))
        return

    def process_IN_MODIFY(self, event):
        if self._pre_path_check(event.pathname):
            return
        if self.logger:
            self.logger.debug('%s: %s' % (event.maskname, event.pathname))
        self.parts_queue.put(self.part_num(event.pathname), True, self.queue_timeout)
        if self.logger:
            self.logger.debug('inotifier watching %s directory.' % len(self.wm.watches))
        return

    def process_IN_ATTRIB(self, event):
        if self._pre_path_check(event.pathname):
            return
        if self.logger:
            self.logger.debug('%s: %s' % (event.maskname, event.pathname))
        self.parts_queue.put(self.part_num(event.pathname), True, self.queue_timeout)
        if self.logger:
            self.logger.debug('inotifier watching %s directory.' % len(self.wm.watches))
        return

    def process_IN_UNMOUNT(self, event):
        if self.logger:
            self.logger.error('%s is not mounted' % event.path)
        return

    def process_IN_Q_OVERFLOW(self, event):
        if self.logger:
            self.logger.error('%s is not mounted' % event.path)
        return

    def _pre_path_check(self, path):
        if basename(path).startswith('tmp') \
                and basename(path).endswith('.tmp'):
            return True
        return False

    def part_num(self, path):
        part = None
        for p in self.paths:
            if path.startswith(p):
                part = path.split(p)[1].split('/')[1]
        try:
            int(part)
        except (TypeError, ValueError), e:
            if self.logger:
                self.logger.warn('%s is an invalid path.' % path)
        return part


class NotifyExcludeFilter(object):
    """
    WatchManage().add_watch() filter
    filtering like
    '/srv/node/sdc1/objects/63829/fd5/3e55582ad0d1b3934a52452d0c3adfd5/'
    (for watching object files like *.data, *.ts and *.meta)
     and
    '/srv/node/sdc1/objects/63829/'
    (for watching new object directory)
    """
    def __call__(self, path):
        if basename(path).startswith('tmp') \
                and basename(path).endswith('.tmp'):
            return True
        path_ls = path.split('/')
        if (len(path_ls) == 6 or len(path_ls) == 8) and isdir(path):
            try:
                int(path_ls[5])
            except:
                return True
            return False
        return True


class ObjectWatcher(object):
    def __init__(self, obj_path, logger=None):
        """
        Watching a path which objects are contained using inotify()
        The watchers are running in independent process asynchronously.

        :param obj_path: absolute path to watch, like '/srv/node/sdb1/objects'
        :param logger: a method of logging
        """
        self.obj_path = obj_path
        self.logger = logger
        self.modified_parts_q = Queue()
        self.newcomer_parts_q = Queue()
        self.run_pause = 2
        self.queue_timeout = 5

        newcomer_notifier = Process(target=self.newcomer_notify,
                                    args=(self.obj_path,))
        newcomer_notifier.daemon = True
        newcomer_notifier.start()
        notifier = Process(target=self.partitions_notify,
                           args=(self.obj_path,))
        notifier.daemon = True
        notifier.start()

    def __call__(self):
        """
        :return list of the partition numbers which modified
                or added from when this method had called last.
        """
        picked_parts = set([])        
        for i in range(self.modified_parts_q.qsize()):
            picked_parts.add(self.modified_parts_q.get(self.queue_timeout))
        return list(picked_parts)


    def partitions_notify(self, obj_path):
        """
        partition's directory watcher with inotify().
        - notify on /srv/node/XXX/objects/PART/XXX/XXXXXXX. It's a timing
          when an existing object modified, deleted or overwritten.
        """
        wm = WatchManager()
        mask = IN_DELETE | IN_CREATE | IN_MODIFY | IN_ATTRIB | \
            IN_Q_OVERFLOW | IN_UNMOUNT
        notifier = Notifier(wm, NotifyProc(parts_queue=self.modified_parts_q,
                                           paths=[obj_path],
                                           watch_manager=wm,
                                           add_watch_part=True,
                                           mask=mask,
                                           logger=self.logger),
                            read_freq=2, timeout=1)
        for part in os.listdir(obj_path):
            part_path = join(obj_path, part)
            wm.add_watch(part_path, mask, rec=True, auto_add=True,
                         do_glob=False,
                         exclude_filter=NotifyExcludeFilter())
        if self.logger:
            self.logger.info('replicate inotifier watching %s directory.' % len(wm.watches))
        while True:
            notifier.process_events()
            if notifier.check_events(timeout=1):
                notifier.read_events()
            if self.newcomer_parts_q.qsize():
                new_part = self.newcomer_parts_q.get(self.queue_timeout)
                part_path = join(obj_path, new_part)
                wm.add_watch(part_path, mask, rec=True, auto_add=True,
                             do_glob=False,
                             exclude_filter=NotifyExcludeFilter())
                self.modified_parts_q.put(new_part, True, self.queue_timeout)
            sleep(self.run_pause)

    def newcomer_notify(self, obj_path):
        """
        partition's directory watcher using inotify()
        - notify IN_CREATE on /srv/node/XXX/objects. It is a Timing when
          new object created.
        """
        wm = WatchManager()
        mask = IN_CREATE | IN_Q_OVERFLOW | IN_UNMOUNT
        notifier = Notifier(wm, NotifyProc(parts_queue=self.newcomer_parts_q,
                                           paths=[obj_path],
                                           watch_manager=wm,
                                           add_watch_part=False,
                                           mask=mask,
                                           logger=self.logger),
                            read_freq=2, timeout=1)
        wm.add_watch(obj_path, mask, rec=False, auto_add=False,
                     do_glob=False)
        while True:
            notifier.process_events()
            if notifier.check_events(timeout=1):
                notifier.read_events()
            sleep(self.run_pause)


if __name__ == '__main__':
    pw = ObjectWatcher('/srv/node/swift0/objects')
    parts = set([])
    while True:
        new_parts = pw()
        [parts.add(i) for i in new_parts]
        print 'parts: %s' % parts
        sleep(30)
