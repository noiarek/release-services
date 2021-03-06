# -*- coding: utf-8 -*-
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import asyncio
import atexit
import io
import json
import os
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor

import hglib

from cli_common.log import get_logger
from cli_common.mercurial import batch_checkout
from cli_common.phabricator import BuildState
from cli_common.phabricator import UnitResult
from cli_common.phabricator import UnitResultState
from pulselistener.config import REPO_TRY

logger = get_logger(__name__)

TREEHERDER_URL = 'https://treeherder.mozilla.org/#/jobs?repo=try&revision={}'


class MercurialWorker(object):
    '''
    Mercurial worker maintaining a local clone of mozilla-unified
    '''
    def __init__(self, phabricator_api, ssh_user, ssh_key, repo_url, repo_dir, batch_size, publish_phabricator):
        self.repo_url = repo_url
        self.repo_dir = repo_dir
        self.phabricator_api = phabricator_api
        self.batch_size = batch_size
        self.publish_phabricator = publish_phabricator
        logger.info('Phabricator publication is {}'.format(self.publish_phabricator and 'enabled' or 'disabled'))  # noqa

        # Build asyncio shared queue
        self.queue = asyncio.Queue()

        # Write ssh key from secret
        _, self.ssh_key_path = tempfile.mkstemp(suffix='.key')
        with open(self.ssh_key_path, 'w') as f:
            f.write(ssh_key)

        # Build ssh conf
        conf = {
            'StrictHostKeyChecking': 'no',
            'User': ssh_user,
            'IdentityFile': self.ssh_key_path,
        }
        self.ssh_conf = 'ssh {}'.format(' '.join('-o {}="{}"'.format(k, v) for k, v in conf.items())).encode('utf-8')

        # Remove key when finished
        atexit.register(self.cleanup)

    def cleanup(self):
        os.unlink(self.ssh_key_path)
        logger.info('Removed ssh key')

    async def run(self):
        # Start by updating the repo in a separate process
        loop = asyncio.get_running_loop()
        with ProcessPoolExecutor() as pool:
            logger.info('Checking out tip in a separate process', repo=self.repo_url)
            await loop.run_in_executor(
                pool,
                batch_checkout,
                self.repo_url, self.repo_dir, b'tip', self.batch_size,
            )
            logger.info('Batch checkout finished')

        # Setup repo in main process
        self.repo = hglib.open(self.repo_dir)
        self.repo.setcbout(lambda msg: logger.info('Mercurial', stdout=msg))
        self.repo.setcberr(lambda msg: logger.info('Mercurial', stderr=msg))

        # Wait for phabricator diffs to apply
        while True:
            build_target_phid, diff = await self.queue.get()
            assert isinstance(diff, dict)
            assert 'phid' in diff

            await self.handle_build(build_target_phid, diff)

            # Notify the queue that the message has been processed
            self.queue.task_done()

    async def handle_build(self, build_target_phid, diff):
        '''
        Try to load and apply a diff on local clone
        If succesful, push to try and send a treeherder link
        If failure, send a unit result with a warning message
        '''
        failure = None
        start = time.time()
        try:
            await self.push_to_try(build_target_phid, diff)
        except hglib.error.CommandError as e:
            # Format nicely the error log
            error_log = e.err
            if isinstance(error_log, bytes):
                error_log = error_log.decode('utf-8')

            logger.warn('Mercurial error on diff', error=error_log, args=e.args, phid=diff['phid'])

            # Report mercurial failure as a Unit Test issue
            failure = UnitResult(
                namespace='code-review',
                name='mercurial',
                result=UnitResultState.Fail,
                details='WARNING: The code review bot failed to apply your patch.\n\n```{}```'.format(error_log),
                format='remarkup',
                duration=time.time() - start,
            )

        except Exception as e:
            logger.warn('Failed to process diff', error=e, phid=diff['phid'])

            # Report generic failure as a Unit Test issue
            failure = UnitResult(
                namespace='code-review',
                name='general',
                result=UnitResultState.Broken,
                details='WARNING: An error occured in the code review bot.\n\n```{}```'.format(e),
                format='remarkup',
                duration=time.time() - start,
            )

        if failure is not None:
            # Remove uncommited changes
            self.repo.revert(self.repo_dir.encode('utf-8'), all=True)

            # Publish failure
            if self.publish_phabricator:
                self.phabricator_api.update_build_target(build_target_phid, BuildState.Fail, unit=[failure])

            return False

        return True

    def clean(self):
        '''
        Steps to clean the mercurial repo
        '''
        logger.info('Remove all mercurial drafts')
        try:
            cmd = hglib.util.cmdbuilder(b'strip', rev=b'roots(outgoing())', force=True, backup=False)
            self.repo.rawcommand(cmd)
        except hglib.error.CommandError as e:
            if b'abort: empty revision set' not in e.err:
                raise

        logger.info('Pull updates from remote repo')
        self.repo.pull()

    async def push_to_try(self, build_target_phid, diff):
        '''
        Handle a new diff received from Phabricator:
        - apply revision to mercurial repo
        - build a custom try_task_config.json
        - trigger push-to-try
        '''
        logger.info('Received diff {phid}'.format(**diff))
        await asyncio.sleep(2)  # allow other tasks to run

        # Start by cleaning the repo
        self.clean()

        # Get the stack of patches
        base, patches = self.phabricator_api.load_patches_stack(self.repo, diff, default_revision='central')
        assert len(patches) > 0, 'No patches to apply'

        # Load all the diffs details with commits messages
        diffs = self.phabricator_api.search_diffs(
            diff_phid=[p[0] for p in patches],
            attachments={
                'commits': True,
            }
        )
        commits = {
            diff['phid']: diff['attachments']['commits'].get('commits', [])
            for diff in diffs
        }

        # Apply the patches and commit them one by one
        for diff_phid, patch in patches:
            commit = commits.get(diff_phid)
            message = ''
            if commit:
                message += '{}\n'.format(commit[0]['message'])
            message += 'Differential Diff: {}'.format(diff_phid)

            logger.info('Applying patch', phid=diff_phid, message=message)
            self.repo.import_(
                patches=io.BytesIO(patch.encode('utf-8')),
                message=message,
                user='pulselistener',
            )
            await asyncio.sleep(1)

        # Build and commit try_task_config.json
        config_path = os.path.join(self.repo_dir, 'try_task_config.json')
        config = {
            'version': 2,
            'parameters': {
                'target_tasks_method': 'codereview',
                'optimize_target_tasks': True,
                'phabricator_diff': build_target_phid or diff['phid'],
            }
        }
        with open(config_path, 'w') as f:
            json.dump(config, f, sort_keys=True, indent=4)
        self.repo.add(config_path.encode('utf-8'))
        self.repo.commit(
            message='try_task_config for code-review\nDifferential Diff: {}'.format(diff['phid']),
            user='pulselistener',
        )

        # Push the commits on try
        commit = self.repo.tip()
        assert commit.node != base.node, 'Commit is the same as base ({}), nothing changed !'.format(commit.node)
        logger.info('Pushing patches to try', rev=commit.node)
        self.repo.push(
            dest=REPO_TRY,
            rev=commit.node,
            ssh=self.ssh_conf,
            force=True,
        )

        logger.info('Diff has been pushed !')

        # Publish Treeherder link
        if build_target_phid and self.publish_phabricator:
            uri = TREEHERDER_URL.format(commit.node.decode('utf-8'))
            self.phabricator_api.create_harbormaster_uri(build_target_phid, 'treeherder', 'Treeherder Jobs', uri)
