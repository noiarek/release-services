# -*- coding: utf-8 -*-
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import itertools
import json
import os.path
import time
from contextlib import contextmanager

import pytest
import responses

from cli_common.phabricator import PhabricatorAPI

MOCK_DIR = os.path.join(os.path.dirname(__file__), 'mocks')


@pytest.fixture(scope='class')
@responses.activate
def mock_config():
    '''
    Mock configuration for bot
    Using try source
    '''
    os.environ['TRY_TASK_ID'] = 'remoteTryTask'
    os.environ['TRY_TASK_GROUP_ID'] = 'remoteTryGroup'

    path = os.path.join(MOCK_DIR, 'config.yaml')
    responses.add(
        responses.GET,
        'https://hg.mozilla.org/mozilla-central/raw-file/tip/tools/clang-tidy/config.yaml',
        body=open(path).read(),
        content_type='text/plain',
    )

    from static_analysis_bot.config import settings
    settings.config = None
    settings.setup('test', 'IN_PATCH', ['dom/*', 'tests/*.py', 'test/*.c'])
    return settings


@pytest.fixture
def mock_issues():
    '''
    Build a list of dummy issues
    '''

    class MockIssue(object):
        def __init__(self, nb):
            self.nb = nb
            self.path = '/path/to/file'

        def as_markdown(self):
            return 'This is the mock issue n°{}'.format(self.nb)

        def as_text(self):
            return str(self.nb)

        def as_dict(self):
            return {
                'nb': self.nb,
            }

        def is_publishable(self):
            return self.nb % 2 == 0

    return [
        MockIssue(i)
        for i in range(5)
    ]


@pytest.fixture
@responses.activate
@contextmanager
def mock_phabricator(mock_config):
    '''
    Mock phabricator authentication process
    '''
    def _response(name):
        path = os.path.join(MOCK_DIR, 'phabricator_{}.json'.format(name))
        assert os.path.exists(path)
        return open(path).read()

    responses.add(
        responses.POST,
        'http://phabricator.test/api/user.whoami',
        body=_response('auth'),
        content_type='application/json',
    )

    responses.add(
        responses.POST,
        'http://phabricator.test/api/differential.diff.search',
        body=_response('diff_search'),
        content_type='application/json',
    )

    responses.add(
        responses.POST,
        'http://phabricator.test/api/differential.revision.search',
        body=_response('revision_search'),
        content_type='application/json',
    )

    responses.add(
        responses.POST,
        'http://phabricator.test/api/differential.query',
        body=_response('diff_query'),
        content_type='application/json',
    )

    responses.add(
        responses.POST,
        'http://phabricator.test/api/differential.getrawdiff',
        body=_response('diff_raw'),
        content_type='application/json',
    )

    responses.add(
        responses.POST,
        'http://phabricator.test/api/differential.createinline',
        body=_response('createinline'),
        content_type='application/json',
    )

    responses.add(
        responses.POST,
        'http://phabricator.test/api/edge.search',
        body=_response('edge_search'),
        content_type='application/json',
    )

    responses.add(
        responses.POST,
        'http://phabricator.test/api/transaction.search',
        body=_response('transaction_search'),
        content_type='application/json',
    )

    yield PhabricatorAPI(
        url='http://phabricator.test/api/',
        api_key='deadbeef',
    )


@pytest.fixture
def mock_stats(mock_config):
    '''
    Mock Datadog authentication and stats management
    '''
    from static_analysis_bot import stats

    # Configure Datadog with a dummy token
    # and an ultra fast flushing cycle
    stats.auth('test_token')
    stats.api.stop()
    stats.api.start(flush_interval=0.001)
    assert not stats.api._disabled
    assert stats.api._is_auto_flushing

    class MemoryReporter(object):
        '''
        A reporting class that reports to memory for testing.
        Used in datadog unit tests:
        https://github.com/DataDog/datadogpy/blob/master/tests/unit/threadstats/test_threadstats.py
        '''
        def __init__(self, api):
            self.metrics = []
            self.events = []
            self.api = api

        def flush_metrics(self, metrics):
            self.metrics += metrics

        def flush_events(self, events):
            self.events += events

        def flush(self):
            # Helper for unit tests to force flush
            self.api.flush(time.time() + 20)

        def get_metrics(self, metric_name):
            return list(itertools.chain(*[
                [
                    [t, point * m['interval']]
                    for t, point in m['points']
                ]
                for m in self.metrics
                if m['metric'] == metric_name
            ]))

    # Gives reporter access to unit tests to access metrics
    stats.api.reporter = MemoryReporter(stats.api)
    yield stats.api.reporter


@pytest.fixture
@responses.activate
def mock_revision(mock_phabricator, mock_config):
    '''
    Mock a mercurial revision
    '''
    from static_analysis_bot.revisions import PhabricatorRevision
    with mock_phabricator as api:
        return PhabricatorRevision(api, diff_phid='PHID-DIFF-XXX')


@pytest.fixture
@responses.activate
def mock_workflow(tmpdir, mock_phabricator, mock_config):
    '''
    Mock the top level workflow
    '''
    from static_analysis_bot.workflows.base import Workflow

    with mock_phabricator as api:
        return Workflow(
            reporters={},
            index_service=None,
            queue_service=None,
            phabricator_api=api,
        )


@pytest.fixture
def mock_coverage_artifact():
    path = os.path.join(MOCK_DIR, 'zero_coverage_report.json')
    return {
        'public/zero_coverage_report.json': json.load(open(path)),
    }
