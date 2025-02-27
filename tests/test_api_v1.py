# SPDX-License-Identifier: GPL-3.0-or-later
import json
from unittest import mock

import kombu.exceptions
import pytest

from cachito.web.models import Request, EnvironmentVariable
from cachito.workers.tasks import (
    fetch_app_source, fetch_gomod_source, set_request_state, failed_request_callback,
    create_bundle_archive,
)


@mock.patch('cachito.web.api_v1.chain')
def test_create_and_fetch_request(mock_chain, app, auth_env, client, db):
    data = {
        'repo': 'https://github.com/release-engineering/retrodep.git',
        'ref': 'c50b93a32df1c9d700e3e80996845bc2e13be848',
        'pkg_managers': ['gomod']
    }

    with mock.patch.dict(app.config, {'LOGIN_DISABLED': False}):
        rv = client.post(
            '/api/v1/requests', json=data, environ_base=auth_env)
    assert rv.status_code == 201
    created_request = json.loads(rv.data.decode('utf-8'))
    for key, expected_value in data.items():
        assert expected_value == created_request[key]
    assert created_request['user'] == 'tbrady@domain.local'

    error_callback = failed_request_callback.s(1)
    mock_chain.assert_called_once_with(
        fetch_app_source.s(
            'https://github.com/release-engineering/retrodep.git',
            'c50b93a32df1c9d700e3e80996845bc2e13be848',
            request_id_to_update=1,
        ).on_error(error_callback),
        fetch_gomod_source.s(request_id_to_update=1).on_error(error_callback),
        create_bundle_archive.s(request_id=1).on_error(error_callback),
        set_request_state.si(1, 'complete', 'Completed successfully'),
    )

    request_id = created_request['id']
    rv = client.get('/api/v1/requests/{}'.format(request_id))
    assert rv.status_code == 200
    fetched_request = json.loads(rv.data.decode('utf-8'))

    assert created_request == fetched_request
    assert fetched_request['state'] == 'in_progress'
    assert fetched_request['state_reason'] == 'The request was initiated'


@mock.patch('cachito.web.api_v1.chain')
def test_fetch_paginated_requests(mock_chain, app, auth_env, client, db):

    repo_template = 'https://github.com/release-engineering/retrodep{}.git'
    # flask_login.current_user is used in Request.from_json, which requires a request context
    with app.test_request_context(environ_base=auth_env):
        for i in range(50):
            data = {
                'repo': repo_template.format(i),
                'ref': 'c50b93a32df1c9d700e3e80996845bc2e13be848',
                'pkg_managers': ['gomod'],
            }
            request = Request.from_json(data)
            db.session.add(request)
    db.session.commit()

    # Sane defaults are provided
    rv = client.get('/api/v1/requests')
    assert rv.status_code == 200
    response = json.loads(rv.data.decode('utf-8'))
    fetched_requests = response['items']
    assert len(fetched_requests) == 20
    for repo_number, request in enumerate(fetched_requests):
        assert request['repo'] == repo_template.format(repo_number)

    # per_page and page parameters are honored
    rv = client.get('/api/v1/requests?page=2&per_page=10')
    assert rv.status_code == 200
    response = json.loads(rv.data.decode('utf-8'))
    fetched_requests = response['items']
    assert len(fetched_requests) == 10
    # Start at 10 because each page contains 10 items and we're processing the second page
    for repo_number, request in enumerate(fetched_requests, 10):
        assert request['repo'] == repo_template.format(repo_number)


def test_create_request_invalid_ref(auth_env, client, db):
    data = {
        'repo': 'https://github.com/release-engineering/retrodep.git',
        'ref': 'not-a-ref',
        'pkg_managers': ['gomod']
    }

    rv = client.post('/api/v1/requests', json=data, environ_base=auth_env)
    assert rv.status_code == 400
    error = json.loads(rv.data.decode('utf-8'))
    assert error['error'] == 'The "ref" parameter must be a 40 character hex string'


def test_create_request_not_an_object(auth_env, client, db):
    rv = client.post('/api/v1/requests', json=None, environ_base=auth_env)
    assert rv.status_code == 400
    error = json.loads(rv.data.decode('utf-8'))
    assert error['error'] == 'The input data must be a JSON object'


def test_create_request_invalid_parameter(auth_env, client, db):
    data = {
        'repo': 'https://github.com/release-engineering/retrodep.git',
        'ref': 'c50b93a32df1c9d700e3e80996845bc2e13be848',
        'pkg_managers': ['gomod'],
        'user': 'uncle_sam',
    }

    rv = client.post('/api/v1/requests', json=data, environ_base=auth_env)
    assert rv.status_code == 400
    error = json.loads(rv.data.decode('utf-8'))
    assert error['error'] == 'The following parameters are invalid: user'


def test_create_request_not_logged_in(client, db):
    data = {
        'repo': 'https://github.com/release-engineering/retrodep.git',
        'ref': 'c50b93a32df1c9d700e3e80996845bc2e13be848',
        'pkg_managers': ['gomod'],
    }

    rv = client.post('/api/v1/requests', json=data)
    assert rv.status_code == 401
    error = json.loads(rv.data.decode('utf-8'))
    assert error['error'] == (
        'The server could not verify that you are authorized to access the URL requested. You '
        'either supplied the wrong credentials (e.g. a bad password), or your browser doesn\'t '
        'understand how to supply the credentials required.'
    )


def test_missing_request(client, db):
    rv = client.get('/api/v1/requests/1')
    assert rv.status_code == 404

    rv = client.get('/api/v1/requests/1/download')
    assert rv.status_code == 404


def test_malformed_request_id(client, db):
    rv = client.get('/api/v1/requests/spam')
    assert rv.status_code == 404
    data = json.loads(rv.data.decode('utf-8'))
    assert data == {'error': 'The requested resource was not found'}


@pytest.mark.parametrize('removed_params', (
    ('repo', 'ref', 'pkg_managers'),
    ('repo',),
    ('ref',),
    ('pkg_managers',),
))
def test_validate_required_params(auth_env, client, db, removed_params):
    data = {
        'repo': 'https://github.com/release-engineering/retrodep.git',
        'ref': 'c50b93a32df1c9d700e3e80996845bc2e13be848',
        'pkg_managers': ['gomod']
    }
    for removed_param in removed_params:
        data.pop(removed_param)

    rv = client.post('/api/v1/requests', json=data, environ_base=auth_env)
    assert rv.status_code == 400
    error_msg = json.loads(rv.data.decode('utf-8'))['error']
    assert 'Missing required' in error_msg
    for removed_param in removed_params:
        assert removed_param in error_msg


def test_validate_extraneous_params(auth_env, client, db):
    data = {
        'repo': 'https://github.com/release-engineering/retrodep.git',
        'ref': 'c50b93a32df1c9d700e3e80996845bc2e13be848',
        'pkg_managers': ['gomod'],
        'spam': 'maps',
    }

    rv = client.post('/api/v1/requests', json=data, environ_base=auth_env)
    assert rv.status_code == 400
    error_msg = json.loads(rv.data.decode('utf-8'))['error']
    assert error_msg == 'The following parameters are invalid: spam'


@mock.patch('cachito.web.api_v1.chain')
def test_create_request_connection_error(mock_chain, app, auth_env, client, db):
    data = {
        'repo': 'https://github.com/release-engineering/retrodep.git',
        'ref': 'c50b93a32df1c9d700e3e80996845bc2e13be848',
        'pkg_managers': ['gomod']
    }

    mock_chain.side_effect = kombu.exceptions.OperationalError('Failed to connect')
    with mock.patch.dict(app.config, {'LOGIN_DISABLED': False}):
        rv = client.post('/api/v1/requests', json=data, environ_base=auth_env)

    assert rv.status_code == 500
    assert rv.json == {'error': 'Failed to connect to the broker to schedule a task'}


@mock.patch('os.path.exists')
@mock.patch('flask.send_file')
@mock.patch('cachito.web.api_v1.Request')
def test_download_archive(mock_request, mock_send_file, mock_exists, client, app):
    bundle_archive_path = '/tmp/cachito-archives/bundles/1.tar.gz'
    mock_request.query.get_or_404().last_state.state_name = 'complete'
    mock_request.query.get_or_404().bundle_archive = bundle_archive_path
    mock_exists.return_value = True
    mock_send_file.return_value = 'something'
    client.get('/api/v1/requests/1/download')
    mock_send_file.assert_called_once_with(bundle_archive_path, mimetype='application/gzip')


@mock.patch('os.path.exists')
@mock.patch('cachito.web.api_v1.Request')
def test_download_archive_no_bundle(mock_request, mock_exists, client, app):
    mock_request.query.get_or_404().last_state.state_name = 'complete'
    mock_exists.return_value = False
    rv = client.get('/api/v1/requests/1/download')
    assert rv.status_code == 500


@mock.patch('cachito.web.api_v1.Request')
def test_download_archive_not_complete(mock_request, client, db, app):
    mock_request.query.get_or_404().last_state.state_name = 'in_progress'
    rv = client.get('/api/v1/requests/1/download')
    assert rv.status_code == 400
    assert rv.json == {
        'error': 'The request must be in the "complete" state before downloading the archive',
    }


@pytest.mark.parametrize('state', ('complete', 'failed'))
@mock.patch('os.path.exists')
@mock.patch('shutil.rmtree')
def test_set_state(mock_rmtree, mock_exists, state, app, client, db, worker_auth_env):
    mock_exists.return_value = True
    data = {
        'repo': 'https://github.com/release-engineering/retrodep.git',
        'ref': 'c50b93a32df1c9d700e3e80996845bc2e13be848',
        'pkg_managers': ['gomod'],
    }
    # flask_login.current_user is used in Request.from_json, which requires a request context
    with app.test_request_context(environ_base=worker_auth_env):
        request = Request.from_json(data)
    db.session.add(request)
    db.session.commit()

    state = state
    state_reason = 'Some status'
    payload = {'state': state, 'state_reason': state_reason}
    patch_rv = client.patch('/api/v1/requests/1', json=payload, environ_base=worker_auth_env)
    assert patch_rv.status_code == 200

    get_rv = client.get('/api/v1/requests/1')
    assert get_rv.status_code == 200

    fetched_request = json.loads(get_rv.data.decode('utf-8'))
    assert fetched_request['state'] == state
    assert fetched_request['state_reason'] == state_reason
    # Since the date is always changing, the actual value can't be confirmed
    assert fetched_request['updated']
    assert len(fetched_request['state_history']) == 2
    # Make sure the order is from newest to oldest
    assert fetched_request['state_history'][0]['state'] == state
    assert fetched_request['state_history'][0]['state_reason'] == state_reason
    assert fetched_request['state_history'][0]['updated']
    assert fetched_request['state_history'][1]['state'] == 'in_progress'
    mock_exists.assert_called_once_with('/tmp/cachito-archives/bundles/temp/1')
    mock_rmtree.assert_called_once_with('/tmp/cachito-archives/bundles/temp/1')


@pytest.mark.parametrize('bundle_exists', (True, False))
@mock.patch('os.path.exists')
@mock.patch('os.remove')
def test_set_state_stale(mock_remove, mock_exists, bundle_exists, app, client, db, worker_auth_env):
    mock_exists.return_value = bundle_exists
    data = {
        'repo': 'https://github.com/release-engineering/retrodep.git',
        'ref': 'c50b93a32df1c9d700e3e80996845bc2e13be848',
        'pkg_managers': ['gomod'],
    }
    # flask_login.current_user is used in Request.from_json, which requires a request context
    with app.test_request_context(environ_base=worker_auth_env):
        request = Request.from_json(data)
    db.session.add(request)
    db.session.commit()

    state = 'stale'
    state_reason = 'The request has expired'
    payload = {'state': state, 'state_reason': state_reason}
    patch_rv = client.patch('/api/v1/requests/1', json=payload, environ_base=worker_auth_env)
    assert patch_rv.status_code == 200

    get_rv = client.get('/api/v1/requests/1')
    assert get_rv.status_code == 200

    fetched_request = get_rv.get_json()
    assert fetched_request['state'] == state
    assert fetched_request['state_reason'] == state_reason
    if bundle_exists:
        mock_remove.assert_called_once_with('/tmp/cachito-archives/bundles/1.tar.gz')
    else:
        mock_remove.assert_not_called()


def test_set_state_from_stale(app, client, db, worker_auth_env):
    data = {
        'repo': 'https://github.com/release-engineering/retrodep.git',
        'ref': 'c50b93a32df1c9d700e3e80996845bc2e13be848',
        'pkg_managers': ['gomod'],
    }
    # flask_login.current_user is used in Request.from_json, which requires a request context
    with app.test_request_context(environ_base=worker_auth_env):
        request = Request.from_json(data)
    db.session.add(request)
    db.session.commit()
    request.add_state('stale', 'The request has expired')
    db.session.commit()

    payload = {'state': 'complete', 'state_reason': 'Unexpired'}
    patch_rv = client.patch('/api/v1/requests/1', json=payload, environ_base=worker_auth_env)
    assert patch_rv.status_code == 400
    assert patch_rv.get_json() == {'error': 'A stale request cannot change states'}


def test_set_state_no_duplicate(app, client, db, worker_auth_env):
    data = {
        'repo': 'https://github.com/release-engineering/retrodep.git',
        'ref': 'c50b93a32df1c9d700e3e80996845bc2e13be848',
        'pkg_managers': ['gomod'],
    }
    # flask_login.current_user is used in Request.from_json, which requires a request context
    with app.test_request_context(environ_base=worker_auth_env):
        request = Request.from_json(data)
    db.session.add(request)
    db.session.commit()

    state = 'complete'
    state_reason = 'Completed successfully'
    payload = {'state': state, 'state_reason': state_reason}
    for i in range(3):
        patch_rv = client.patch('/api/v1/requests/1', json=payload, environ_base=worker_auth_env)
        assert patch_rv.status_code == 200

    get_rv = client.get('/api/v1/requests/1')
    assert get_rv.status_code == 200

    fetched_request = json.loads(get_rv.data.decode('utf-8'))
    # Make sure no duplicate states were added
    assert len(fetched_request['state_history']) == 2


@pytest.mark.parametrize('env_vars', (
    {},
    {'spam': 'maps'},
))
def test_set_deps(app, client, db, worker_auth_env, sample_deps, env_vars):
    data = {
        'repo': 'https://github.com/release-engineering/retrodep.git',
        'ref': 'c50b93a32df1c9d700e3e80996845bc2e13be848',
        'pkg_managers': ['gomod'],
    }
    # flask_login.current_user is used in Request.from_json, which requires a request context
    with app.test_request_context(environ_base=worker_auth_env):
        request = Request.from_json(data)
    db.session.add(request)
    db.session.commit()

    payload = {'dependencies': sample_deps, 'environment_variables': env_vars}
    patch_rv = client.patch('/api/v1/requests/1', json=payload, environ_base=worker_auth_env)
    assert patch_rv.status_code == 200

    len(EnvironmentVariable.query.all()) == len(env_vars.items())
    for name, value in env_vars.items():
        env_var_obj = EnvironmentVariable.query.filter_by(name=name, value=value).first()
        assert env_var_obj

    get_rv = client.get('/api/v1/requests/1')
    assert get_rv.status_code == 200
    fetched_request = json.loads(get_rv.data.decode('utf-8'))
    assert fetched_request['dependencies'] == sample_deps
    assert fetched_request['environment_variables'] == env_vars


def test_set_state_not_logged_in(client, db):
    payload = {'state': 'complete', 'state_reason': 'Completed successfully'}
    rv = client.patch('/api/v1/requests/1', json=payload)
    assert rv.status_code == 401
    error = json.loads(rv.data.decode('utf-8'))
    assert error['error'] == (
        'The server could not verify that you are authorized to access the URL requested. You '
        'either supplied the wrong credentials (e.g. a bad password), or your browser doesn\'t '
        'understand how to supply the credentials required.'
    )


@pytest.mark.parametrize('request_id, payload, status_code, message', (
    (
        1,
        {'state': 'call_for_support', 'state_reason': 'It broke'},
        400,
        'The state "call_for_support" is invalid. It must be one of: complete, failed, '
        'in_progress, stale.',
    ),
    (
        1337,
        {'state': 'complete', 'state_reason': 'Success'},
        404,
        'The requested resource was not found',
    ),
    (
        1,
        {},
        400,
        'At least one key must be specified to update the request',
    ),
    (
        1,
        {'state': 'complete', 'state_reason': 'Success', 'pkg_managers': ['javascript']},
        400,
        'The following keys are not allowed: pkg_managers',
    ),
    (
        1,
        {'state': 1, 'state_reason': 'Success'},
        400,
        'The value for "state" must be a string',
    ),
    (
        1,
        {'state': 'complete'},
        400,
        'The "state_reason" key is required when "state" is supplied',
    ),
    (
        1,
        {'state_reason': 'Success'},
        400,
        'The "state" key is required when "state_reason" is supplied',
    ),
    (
        1,
        'some string',
        400,
        'The input data must be a JSON object',
    ),
    (
        1,
        {'dependencies': 'test'},
        400,
        'The value for "dependencies" must be an array',
    ),
    (
        1,
        {'dependencies': ['test']},
        400,
        'A dependency must be a JSON object with the keys name, type, and version',
    ),
    (
        1,
        {'dependencies': [{'type': 'gomod', 'version': 'v1.4.2'}]},
        400,
        'A dependency must be a JSON object with the keys name, type, and version',
    ),
    (
        1,
        {
            'dependencies': [
                {
                    'name': 'github.com/Masterminds/semver',
                    'type': 'gomod',
                    'version': 3.0,
                },
            ],
        },
        400,
        'The "version" key of the dependency must be a string',
    ),
    (
        1,
        {
            'environment_variables': 'spam',
        },
        400,
        'The value for "environment_variables" must be an object',
    ),
    (
        1,
        {
            'environment_variables': {'spam': None},
        },
        400,
        'The value of environment variables must be a string',
    ),
    (
        1,
        {
            'environment_variables': {'spam': ['maps']},
        },
        400,
        'The value of environment variables must be a string',
    ),
))
def test_state_change_invalid(
    app, client, db, worker_auth_env, request_id, payload, status_code, message
):
    data = {
        'repo': 'https://github.com/release-engineering/retrodep.git',
        'ref': 'c50b93a32df1c9d700e3e80996845bc2e13be848',
        'pkg_managers': ['gomod'],
    }
    # flask_login.current_user is used in Request.from_json, which requires a request context
    with app.test_request_context(environ_base=worker_auth_env):
        request = Request.from_json(data)
    db.session.add(request)
    db.session.commit()

    rv = client.patch(f'/api/v1/requests/{request_id}', json=payload, environ_base=worker_auth_env)
    assert rv.status_code == status_code
    data = json.loads(rv.data.decode('utf-8'))
    assert data == {'error': message}
