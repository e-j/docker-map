# -*- coding: utf-8 -*-
from __future__ import absolute_import, unicode_literals

from collections import defaultdict

import posixpath
import unittest
import responses

from dockermap import DEFAULT_COREIMAGE, DEFAULT_BASEIMAGE
from dockermap.map.config.client import ClientConfiguration
from dockermap.map.config.host_volume import get_host_path
from dockermap.map.config.main import ContainerMap, expand_instances
from dockermap.map.input import (ExecCommand, EXEC_POLICY_INITIAL, EXEC_POLICY_RESTART, MapConfigId,
                                 ITEM_TYPE_CONTAINER, ITEM_TYPE_VOLUME, ITEM_TYPE_NETWORK)
from dockermap.map.policy import CONFIG_FLAG_DEPENDENT
from dockermap.map.policy.base import BasePolicy
from dockermap.map.state import (INITIAL_START_TIME, STATE_RUNNING, STATE_PRESENT, STATE_ABSENT,
                                 STATE_FLAG_NONRECOVERABLE, STATE_FLAG_RESTARTING, STATE_FLAG_INITIAL,
                                 STATE_FLAG_NEEDS_RESET, STATE_FLAG_MISC_MISMATCH, STATE_FLAG_IMAGE_MISMATCH,
                                 STATE_FLAG_VOLUME_MISMATCH, STATE_FLAG_FORCED_RESET)
from dockermap.map.state.base import DependencyStateGenerator, DependentStateGenerator, SingleStateGenerator
from dockermap.map.state.update import UpdateStateGenerator
from dockermap.map.state.utils import merge_dependency_paths

from tests import MAP_DATA_2, CLIENT_DATA_1


URL_PREFIX = 'http+docker://localunixsocket/v{0}'.format(CLIENT_DATA_1['version'])

P_STATE_INITIAL = 0
P_STATE_RUNNING = 1
P_STATE_RESTARTING = 2
P_STATE_EXITED_0 = 3
P_STATE_EXITED_127 = 4
STATE_RESULTS = {
    P_STATE_INITIAL: {
        'Running': False,
        'Restarting': False,
        'ExitCode': 0,
        'StartedAt': INITIAL_START_TIME,
    },
    P_STATE_RESTARTING: {
        'Running': False,
        'Restarting': True,
        'ExitCode': 255,
        'StartedAt': "2016-02-05T20:14:04.655843958Z",
    },
    P_STATE_RUNNING: {
        'Running': True,
        'Restarting': False,
        'ExitCode': 0,
        'StartedAt': "2016-02-05T20:14:04.655843958Z",
    },
    P_STATE_EXITED_0: {
        'Running': False,
        'Restarting': False,
        'ExitCode': 0,
        'StartedAt': "2016-02-05T20:14:04.655843958Z",
    },
    P_STATE_EXITED_127: {
        'Running': False,
        'Restarting': False,
        'ExitCode': -127,
        'StartedAt': "2016-02-05T20:14:04.655843958Z",
    },
}


def _container(config_name, p_state=P_STATE_RUNNING, instances=None, attached_volumes_valid=True,
               instance_volumes_valid=True, **kwargs):
    return config_name, p_state, instances, attached_volumes_valid, instance_volumes_valid, kwargs


def _add_container_list(rsps, container_names):
    results = [
        {'Id': '{0}'.format(c_id), 'Names': ['/{0}'.format(name)]}
        for c_id, name in container_names
    ]
    rsps.add('GET', '{0}/containers/json'.format(URL_PREFIX), content_type='application/json', json=results)


def _add_image_list(rsps, image_names):
    image_list = [
        {
            'RepoTags': ['{0}:latest'.format(i_name), '{0}:1.0'.format(i_name)] if ':' not in i_name else [i_name],
            'Id': '{0}'.format(i_id),
        }
        for i_id, i_name in image_names
    ]
    rsps.add('GET', '{0}/images/json'.format(URL_PREFIX), content_type='application/json', json=image_list)
    rsps.add('POST', '{0}/images/create'.format(URL_PREFIX), content_type='application/json')


def _get_container_mounts(config_id, container_map, c_config, valid):
    if valid:
        path_prefix = '/valid'
    else:
        path_prefix = '/invalid_{0}'.format(config_id.config_name)
    for a in c_config.attaches:
        c_path = container_map.volumes[a]
        yield {'Source': posixpath.join(path_prefix, 'attached', a), 'Destination': c_path, 'RW': True}
    if config_id.config_type == ITEM_TYPE_CONTAINER:
        for vol, ro in c_config.binds:
            if isinstance(vol, tuple):
                c_path, h_r_path = vol
                h_path = get_host_path(container_map.host.root, h_r_path, config_id.instance_name)
            else:
                c_path = container_map.volumes[vol]
                h_path = container_map.host.get_path(vol, config_id.instance_name)
            yield {'Source': posixpath.join(path_prefix, h_path), 'Destination': c_path, 'RW': not ro}
        for s in c_config.shares:
            yield {'Source': posixpath.join(path_prefix, 'shared', s), 'Destination': s, 'RW': True}
        for vol, ro in c_config.uses:
            c, __, i = vol.partition('.')
            c_ref = container_map.get_existing(c)
            if i in c_ref.attaches:
                c_path = container_map.volumes[i]
                yield {'Source': posixpath.join(path_prefix, 'attached', i), 'Destination': c_path, 'RW': not ro}
            elif c_ref and (not i or i in c_ref.instances):
                for r_mount in _get_container_mounts(MapConfigId(config_id.config_type, config_id.map_name, c, i),
                                                     container_map, c_ref, valid):
                    yield r_mount
            else:
                raise ValueError("Invalid uses declaration in {0}: {1}".format(config_name, vol))


def _add_inspect(rsps, config_id, container_map, c_config, state, container_id, image_id,
                 volumes_valid, links_valid=True, cmd_valid=True, env_valid=True, **kwargs):
    config_type = config_id.config_type
    if config_type == ITEM_TYPE_CONTAINER:
        if config_id.instance_name:
            container_name = '{0.map_name}.{0.config_name}.{0.instance_name}'.format(config_id)
        else:
            container_name = '{0.map_name}.{0.config_name}'.format(config_id)
    elif config_type == ITEM_TYPE_VOLUME:
        if container_map.use_attached_parent_name:
            container_name = '{0.map_name}.{0.config_name}.{0.instance_name}'.format(config_id)
        else:
            container_name = '{0.map_name}.{0.instance_name}'.format(config_id)
    else:
        raise ValueError(config_type)
    ports = defaultdict(list)
    host_config = {}
    network_settings = {}
    config_dict = {
        'Env': None,
        'Cmd': [],
        'Entrypoint': [],
    }
    if config_type == ITEM_TYPE_CONTAINER:
        for ex in c_config.exposes:
            ex_port = '{0}/tcp'.format(ex.exposed_port)
            if ex.host_port:
                if ex.interface:
                    ip = CLIENT_DATA_1['interfaces'][ex.interface]
                else:
                    ip = '0.0.0.0'
                ports[ex_port].append({
                    'HostIp': ip,
                    'HostPort': '{0}'.format(ex.host_port)
                })
            else:
                ports[ex_port].extend(())
        host_config = {'Links': [
            '/{0}.{1}:/{2}/{3}'.format(config_id.map_name, link.container, container_name,
                                       link.alias or BasePolicy.get_hostname(link.container))
            for link in c_config.links
        ]}
        network_settings = {
            'Ports': ports,
        }
    results = {
        'Id': '{0}'.format(container_id),
        'Names': ['/{0}'.format(container_name)],
        'State': STATE_RESULTS[state],
        'Image': '{0}'.format(image_id),
        'Mounts': list(_get_container_mounts(config_id, container_map, c_config, volumes_valid)),
        'HostConfig': host_config,
        'Config': config_dict,
        'NetworkSettings': network_settings,
    }
    exec_results = {
        'Processes': [
            [cmd_i, cmd.user, cmd.cmd]
            for cmd_i, cmd in enumerate(c_config.exec_commands)
        ],
    }
    results.update(kwargs)
    rsps.add('GET', '{0}/containers/{1}/json'.format(URL_PREFIX, container_name),
             content_type='application/json',
             json=results)
    rsps.add('GET', '{0}/containers/{1}/json'.format(URL_PREFIX, container_id),
             content_type='application/json',
             json=results)
    rsps.add('GET', '{0}/containers/{1}/top'.format(URL_PREFIX, container_name),
             content_type='application/json',
             json=exec_results)
    rsps.add('GET', '{0}/containers/{1}/top'.format(URL_PREFIX, container_id),
             content_type='application/json',
             json=exec_results)
    return container_id, container_name


def _get_single_state(sg, config_ids):
    states = [s
              for s in sg.get_states(config_ids)
              if s.config_id.config_type == ITEM_TYPE_CONTAINER]
    return states[-1]


def _get_states_dict(sl):
    cd = {}
    nd = {}
    vd = {}
    for s in sl:
        config_id = s.config_id
        if config_id.config_type == ITEM_TYPE_CONTAINER:
            cd[(config_id.config_name, config_id.instance_name)] = s
        elif config_id.config_type == ITEM_TYPE_VOLUME:
            vd[(config_id.config_name, config_id.instance_name)] = s
        elif config_id.config_type == ITEM_TYPE_NETWORK:
            nd[config_id.config_name] = s
        else:
            raise ValueError("Invalid configuration type.", s.config_type)
    return {
        'containers': cd,
        'volumes': vd,
        'networks': nd,
    }


class TestPolicyStateGenerators(unittest.TestCase):
    def setUp(self):
        self.map_name = map_name = 'main'
        self.sample_map = sample_map = ContainerMap('main', MAP_DATA_2,
                                                    use_attached_parent_name=True).get_extended_map()
        self.sample_map.repository = None
        self.sample_client_config = client_config = ClientConfiguration(**CLIENT_DATA_1)
        self.policy = BasePolicy({map_name: sample_map}, {'__default__': client_config})
        self.server_config_id = self._config_id('server')
        all_images = set(c_config.image or c_name for c_name, c_config in sample_map)
        all_images.add(DEFAULT_COREIMAGE)
        all_images.add(DEFAULT_BASEIMAGE)
        self.images = list(enumerate(all_images))

    def _config_id(self, config_name, instance=None):
        return [MapConfigId(ITEM_TYPE_CONTAINER, self.map_name, config_name, instance)]

    def _setup_containers(self, rsps, containers_states):
        container_names = []
        _add_image_list(rsps, self.images)
        image_dict = {name: _id for _id, name in self.images}
        container_id = 0
        base_image_id = image_dict[DEFAULT_BASEIMAGE]
        for name, state, instances, attached_valid, instances_valid, kwargs in containers_states:
            c_config = self.sample_map.get_existing(name)
            for a in c_config.attaches:
                container_id += 1
                config_id = MapConfigId(ITEM_TYPE_VOLUME, self.map_name, name, a)
                container_names.append(_add_inspect(rsps, config_id, self.sample_map, c_config,
                                                    P_STATE_EXITED_0, container_id, base_image_id, attached_valid))
            image_id = image_dict[c_config.image or name]
            for i in instances or c_config.instances or [None]:
                container_id += 1
                config_id = MapConfigId(ITEM_TYPE_CONTAINER, self.map_name, name, i)
                container_names.append(_add_inspect(rsps, config_id, self.sample_map, c_config,
                                                    state, container_id, image_id, instances_valid, **kwargs))
        _add_container_list(rsps, container_names)

    def _setup_default_containers(self, rsps):
        self._setup_containers(rsps, [
            _container('sub_sub_svc'),
            _container('sub_svc'),
            _container('redis'),
            _container('svc'),
            _container('server'),
        ])

    def test_dependency_states_running(self):
        with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            self._setup_default_containers(rsps)
            states = list(DependencyStateGenerator(self.policy, {}).get_states(self.server_config_id))
            instance_base_states = [s.base_state
                                    for s in states
                                    if s.config_id.config_type == ITEM_TYPE_CONTAINER]
            attached_base_states = [s.base_state
                                    for s in states
                                    if s.config_id.config_type == ITEM_TYPE_VOLUME]
            self.assertTrue(all(si == STATE_RUNNING
                                for si in instance_base_states))
            self.assertTrue(all(si == STATE_PRESENT
                                for si in attached_base_states))
            self.assertTrue(all(s.config_flags == CONFIG_FLAG_DEPENDENT
                                for s in states
                                if s.config_id.config_type == ITEM_TYPE_CONTAINER and s.config_id.config_name != 'server'))

    def test_single_states_mixed(self):
        with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            self._setup_containers(rsps, [
                _container('redis', P_STATE_EXITED_0, instances=['cache']),
                _container('redis', instances=['queue']),
                _container('svc', P_STATE_EXITED_127),
                _container('worker', P_STATE_RESTARTING),
                _container('worker_q2', P_STATE_INITIAL),
            ])
            sg = SingleStateGenerator(self.policy, {})
            cache_state = _get_single_state(sg, self._config_id('redis', 'cache'))
            self.assertEqual(cache_state.base_state, STATE_PRESENT)
            queue_state = _get_single_state(sg, self._config_id('redis', 'queue'))
            self.assertEqual(queue_state.base_state, STATE_RUNNING)
            svc_state = _get_single_state(sg, self._config_id('svc'))
            self.assertEqual(svc_state.base_state, STATE_PRESENT)
            self.assertEqual(svc_state.state_flags & STATE_FLAG_NONRECOVERABLE, STATE_FLAG_NONRECOVERABLE)
            worker_state = _get_single_state(sg, self._config_id('worker'))
            self.assertEqual(worker_state.state_flags & STATE_FLAG_RESTARTING, STATE_FLAG_RESTARTING)
            worker2_state = _get_single_state(sg, self._config_id('worker_q2'))
            self.assertEqual(worker2_state.base_state, STATE_PRESENT)
            self.assertEqual(worker2_state.state_flags & STATE_FLAG_INITIAL, STATE_FLAG_INITIAL)
            server_states = _get_single_state(sg, self.server_config_id)
            self.assertEqual(server_states.base_state, STATE_ABSENT)

    def test_single_states_forced_config(self):
        with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            self._setup_containers(rsps, [
                _container('redis', instances=['cache', 'queue']),
            ])
            force_update = set(expand_instances(self._config_id('redis'),
                                                ext_map=self.sample_map))
            sg = SingleStateGenerator(self.policy, {'force_update': force_update})
            cache_state = _get_single_state(sg, self._config_id('redis', 'cache'))
            self.assertEqual(cache_state.state_flags & STATE_FLAG_FORCED_RESET, STATE_FLAG_FORCED_RESET)
            queue_state = _get_single_state(sg, self._config_id('redis', 'queue'))
            self.assertEqual(queue_state.state_flags & STATE_FLAG_FORCED_RESET, STATE_FLAG_FORCED_RESET)

    def test_single_states_forced_instance(self):
        with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            self._setup_containers(rsps, [
                _container('redis', instances=['cache', 'queue']),
            ])
            force_update = set(expand_instances(self._config_id('redis', 'cache'),
                                                ext_map=self.sample_map))
            sg = SingleStateGenerator(self.policy, {'force_update': force_update})
            cache_state = _get_single_state(sg, self._config_id('redis', 'cache'))
            self.assertEqual(cache_state.state_flags & STATE_FLAG_FORCED_RESET, STATE_FLAG_FORCED_RESET)
            queue_state = _get_single_state(sg, self._config_id('redis', 'queue'))
            self.assertEqual(queue_state.state_flags & STATE_FLAG_NEEDS_RESET, 0)

    def test_dependent_states(self):
        with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            self._setup_containers(rsps, [
                _container('sub_sub_svc'),
                _container('sub_svc'),
                _container('redis'),
                _container('svc'),
                _container('server'),
                _container('server2'),
                _container('worker'),
                _container('worker_q2'),
            ])
            states = list(DependentStateGenerator(self.policy, {}).get_states(self._config_id('redis', 'cache')))
            instance_base_states = [s.base_state
                                    for s in states
                                    if s.config_id.config_type == ITEM_TYPE_CONTAINER]
            volume_base_states = [s.base_state
                                  for s in states
                                  if s.config_id.config_type == ITEM_TYPE_VOLUME]
            self.assertTrue(all(si == STATE_RUNNING
                                for si in instance_base_states))
            self.assertTrue(all(si == STATE_PRESENT
                                for si in volume_base_states))
            self.assertTrue(all(s.config_flags == CONFIG_FLAG_DEPENDENT
                                for s in states
                                if not (s.config_id.config_type == ITEM_TYPE_CONTAINER and s.config_id.config_name == 'redis')))

    def test_update_states_clean(self):
        with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            self._setup_default_containers(rsps)
            states = list(UpdateStateGenerator(self.policy, {}).get_states(self.server_config_id))
            valid_order = ['sub_sub_svc', 'sub_svc', 'redis', 'server']
            for c_state in states:
                config_id = c_state.config_id
                if config_id.config_type == ITEM_TYPE_CONTAINER:
                    config_name = config_id.config_name
                    if config_name in valid_order:
                        self.assertEqual(valid_order[0], config_name)
                        valid_order.pop(0)
                        self.assertEqual(c_state.base_state, STATE_RUNNING)
                        self.assertEqual(c_state.state_flags, 0)

    def test_update_states_invalid_attached(self):
        with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            self._setup_containers(rsps, [
                _container('sub_sub_svc'),
                _container('sub_svc'),
                _container('redis', attached_volumes_valid=False),
                _container('svc'),
                _container('server'),
            ])
            states = _get_states_dict(UpdateStateGenerator(self.policy, {}).get_states(self.server_config_id))
            server_state = states['containers'][('server', None)]
            self.assertEqual(server_state.base_state, STATE_RUNNING)
            self.assertEqual(server_state.state_flags & STATE_FLAG_VOLUME_MISMATCH, STATE_FLAG_VOLUME_MISMATCH)
            for ri in ('cache', 'queue'):
                redis_state = states['containers'][('redis', ri)]
                self.assertEqual(redis_state.base_state, STATE_RUNNING)
                self.assertEqual(redis_state.state_flags & STATE_FLAG_VOLUME_MISMATCH, STATE_FLAG_VOLUME_MISMATCH)

    def test_update_states_invalid_dependent_instance(self):
        with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            self._setup_containers(rsps, [
                _container('sub_sub_svc'),
                _container('sub_svc'),
                _container('redis', instance_volumes_valid=False),
                _container('svc'),
                _container('server'),
            ])
            states = _get_states_dict(UpdateStateGenerator(self.policy, {}).get_states(self.server_config_id))
            server_state = states['containers'][('server', None)]
            self.assertEqual(server_state.base_state, STATE_RUNNING)
            self.assertEqual(server_state.state_flags & STATE_FLAG_NEEDS_RESET, 0)
            for ri in ('cache', 'queue'):
                redis_state = states['containers'][('redis', ri)]
                self.assertEqual(redis_state.base_state, STATE_RUNNING)
                self.assertEqual(redis_state.state_flags & STATE_FLAG_VOLUME_MISMATCH, STATE_FLAG_VOLUME_MISMATCH)

    def test_update_states_invalid_dependent_instance_attached(self):
        with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            self._setup_containers(rsps, [
                _container('sub_sub_svc'),
                _container('sub_svc'),
                _container('redis'),
                _container('svc'),
                _container('server', attached_volumes_valid=False),
            ])
            states = _get_states_dict(UpdateStateGenerator(self.policy, {}).get_states(self.server_config_id))
            server_state = states['containers'][('server', None)]
            self.assertEqual(server_state.base_state, STATE_RUNNING)
            self.assertEqual(server_state.state_flags & STATE_FLAG_VOLUME_MISMATCH, STATE_FLAG_VOLUME_MISMATCH)
            for ri in ('cache', 'queue'):
                redis_state = states['containers'][('redis', ri)]
                self.assertEqual(redis_state.base_state, STATE_RUNNING)
                self.assertEqual(redis_state.state_flags & STATE_FLAG_NEEDS_RESET, 0)

    def test_update_states_invalid_image(self):
        with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            self._setup_containers(rsps, [
                _container('sub_sub_svc'),
                _container('sub_svc'),
                _container('redis'),
                _container('svc'),
                _container('server', Image='invalid'),
            ])
            states = _get_states_dict(UpdateStateGenerator(self.policy, {}).get_states(self.server_config_id))
            server_state = states['containers'][('server', None)]
            self.assertEqual(server_state.base_state, STATE_RUNNING)
            self.assertEqual(server_state.state_flags & STATE_FLAG_IMAGE_MISMATCH, STATE_FLAG_IMAGE_MISMATCH)

    def test_update_states_invalid_network(self):
        with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            self._setup_containers(rsps, [
                _container('sub_sub_svc'),
                _container('sub_svc'),
                _container('redis'),
                _container('svc'),
                _container('server', NetworkSettings=dict(Ports={})),
            ])
            states = _get_states_dict(UpdateStateGenerator(self.policy, {}).get_states(self.server_config_id))
            server_state = states['containers'][('server', None)]
            self.assertEqual(server_state.base_state, STATE_RUNNING)
            self.assertEqual(server_state.state_flags & STATE_FLAG_MISC_MISMATCH, STATE_FLAG_MISC_MISMATCH)

    def test_update_states_updated_environment(self):
        with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            self._setup_default_containers(rsps)
            self.sample_map.containers['server'].create_options.update(environment=dict(Test='x'))
            states = _get_states_dict(UpdateStateGenerator(self.policy, {}).get_states(self.server_config_id))
            server_state = states['containers'][('server', None)]
            self.assertEqual(server_state.base_state, STATE_RUNNING)
            self.assertEqual(server_state.state_flags & STATE_FLAG_MISC_MISMATCH, STATE_FLAG_MISC_MISMATCH)

    def test_update_states_updated_command(self):
        with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            self._setup_default_containers(rsps)
            self.sample_map.containers['server'].create_options.update(command='/bin/true')
            states = _get_states_dict(UpdateStateGenerator(self.policy, {}).get_states(self.server_config_id))
            server_state = states['containers'][('server', None)]
            self.assertEqual(server_state.base_state, STATE_RUNNING)
            self.assertEqual(server_state.state_flags & STATE_FLAG_MISC_MISMATCH, STATE_FLAG_MISC_MISMATCH)

    def test_update_states_updated_exec(self):
        with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            cmd1 = ExecCommand(2, '/bin/true', EXEC_POLICY_INITIAL)
            cmd2 = ExecCommand(3, '/bin/true', EXEC_POLICY_INITIAL)
            cmd3 = ExecCommand(4, '/bin/true', EXEC_POLICY_RESTART)
            self.sample_map.containers['server'].exec_commands = [cmd1]
            self._setup_default_containers(rsps)
            self.sample_map.containers['server'].exec_commands = [cmd1, cmd2, cmd3]
            states = _get_states_dict(UpdateStateGenerator(self.policy, {}).get_states(self.server_config_id))
            server_state = states['containers'][('server', None)]
            self.assertEqual(server_state.base_state, STATE_RUNNING)
            self.assertEqual(server_state.state_flags & STATE_FLAG_NEEDS_RESET, 0)
            self.assertDictEqual(server_state.extra_data, {'exec_commands': [
                (cmd1, True),
                (cmd2, False),
                (cmd3, False),
            ]})


class TestPolicyStateUtils(unittest.TestCase):
    def setUp(self):
        self.map_name = map_name = 'main'
        self.sample_map = sample_map = ContainerMap('main', MAP_DATA_2,
                                                    use_attached_parent_name=True).get_extended_map()
        # self.sample_map.repository = None
        self.sample_client_config = client_config = ClientConfiguration(**CLIENT_DATA_1)
        self.policy = policy = BasePolicy({map_name: sample_map}, {'__default__': client_config})
        self.state_gen = DependencyStateGenerator(policy, {})
        self.server_dependencies = [
            (ITEM_TYPE_CONTAINER, map_name, 'sub_sub_svc', None),
            (ITEM_TYPE_CONTAINER, map_name, 'sub_svc', None),
            (ITEM_TYPE_VOLUME, map_name, 'redis', 'redis_socket'),
            (ITEM_TYPE_VOLUME, map_name, 'redis', 'redis_log'),
            (ITEM_TYPE_CONTAINER, map_name, 'redis', 'queue'),
            (ITEM_TYPE_CONTAINER, map_name, 'redis', 'cache'),
            (ITEM_TYPE_CONTAINER, map_name, 'svc', None),
            (ITEM_TYPE_VOLUME, map_name, 'server', 'app_log'),
            (ITEM_TYPE_VOLUME, map_name, 'server', 'server_log'),
        ]
        self.redis_dependencies = [
            (ITEM_TYPE_CONTAINER, self.map_name, 'sub_sub_svc', None),
            (ITEM_TYPE_CONTAINER, self.map_name, 'sub_svc', None),
            (ITEM_TYPE_VOLUME, map_name, 'redis', 'redis_socket'),
            (ITEM_TYPE_VOLUME, map_name, 'redis', 'redis_log'),
        ]

    def test_merge_single(self):
        redis_config = self._config_id('redis', 'queue')
        merged_paths = merge_dependency_paths([
            (redis_config, self.state_gen.get_dependency_path(redis_config))
        ])
        self.assertItemsEqual([
            (redis_config, self.redis_dependencies)
        ], merged_paths)

    def test_merge_empty(self):
        svc_config = self._config_id('sub_sub_svc')
        merged_paths = merge_dependency_paths([
            (svc_config, self.state_gen.get_dependency_path(svc_config))
        ])
        self.assertItemsEqual([(svc_config, [])], merged_paths)

    def _config_id(self, config_name, instance=None):
        return MapConfigId(ITEM_TYPE_CONTAINER, self.map_name, config_name, instance)

    def test_merge_two_common(self):
        server_config = self._config_id('server')
        worker_config = self._config_id('worker')
        merged_paths = merge_dependency_paths([
            (c, self.state_gen.get_dependency_path(c))
            for c in [server_config, worker_config]
        ])
        self.assertEqual(len(merged_paths), 2)
        self.assertEqual(merged_paths[0][0], server_config)
        self.assertListEqual(self.server_dependencies, merged_paths[0][1])
        self.assertEqual(merged_paths[1][0], worker_config)
        self.assertListEqual([
            (ITEM_TYPE_VOLUME, self.map_name, 'worker', 'app_log'),
        ], merged_paths[1][1])

    def test_merge_three_common(self):
        server_config = self._config_id('server')
        worker_config = self._config_id('worker')
        worker_q2_config = self._config_id('worker_q2')
        merged_paths = merge_dependency_paths([
            (c, self.state_gen.get_dependency_path(c))
            for c in [server_config, worker_config, worker_q2_config]
        ])
        self.assertEqual(len(merged_paths), 3)
        self.assertEqual(merged_paths[0][0], server_config)
        self.assertEqual(merged_paths[1][0], worker_config)
        self.assertEqual(merged_paths[2][0], worker_q2_config)
        self.assertListEqual(self.server_dependencies, merged_paths[0][1])
        self.assertListEqual([
            (ITEM_TYPE_VOLUME, self.map_name, 'worker', 'app_log'),
        ], merged_paths[1][1])
        self.assertListEqual([
            (ITEM_TYPE_VOLUME, self.map_name, 'worker_q2', 'app_log'),
        ], merged_paths[2][1])

    def test_merge_three_common_with_extension(self):
        worker_config = self._config_id('worker')
        server2_config = self._config_id('server2')
        worker_q2_config = self._config_id('worker_q2')
        merged_paths = merge_dependency_paths([
            (c, self.state_gen.get_dependency_path(c))
            for c in [worker_config, server2_config, worker_q2_config]
        ])
        self.assertEqual(len(merged_paths), 3)
        self.assertEqual(merged_paths[0][0], worker_config)
        self.assertEqual(merged_paths[1][0], server2_config)
        self.assertEqual(merged_paths[2][0], worker_q2_config)
        self.assertListEqual([
            (ITEM_TYPE_CONTAINER, self.map_name, 'sub_sub_svc', None),
            (ITEM_TYPE_CONTAINER, self.map_name, 'sub_svc', None),
            (ITEM_TYPE_VOLUME, self.map_name, 'redis', 'redis_socket'),
            (ITEM_TYPE_VOLUME, self.map_name, 'redis', 'redis_log'),
            (ITEM_TYPE_CONTAINER, self.map_name, 'redis', 'queue'),
            (ITEM_TYPE_CONTAINER, self.map_name, 'redis', 'cache'),
            (ITEM_TYPE_CONTAINER, self.map_name, 'svc', None),
            (ITEM_TYPE_VOLUME, self.map_name, 'worker', 'app_log'),
        ], merged_paths[0][1])
        self.assertListEqual([
            (ITEM_TYPE_CONTAINER, self.map_name, 'svc2', None),
            (ITEM_TYPE_VOLUME, self.map_name, 'server2', 'app_log'),
            (ITEM_TYPE_VOLUME, self.map_name, 'server2', 'server_log'),
        ], merged_paths[1][1])
        self.assertListEqual([
            (ITEM_TYPE_VOLUME, self.map_name, 'worker_q2', 'app_log'),
        ], merged_paths[2][1])

    def test_merge_included_first(self):
        redis_config = self._config_id('redis', 'cache')
        server_config = self._config_id('server')
        merged_paths = merge_dependency_paths([
            (c, self.state_gen.get_dependency_path(c))
            for c in [redis_config, server_config]
        ])
        self.assertEqual(len(merged_paths), 1)
        self.assertEqual(merged_paths[0][0], server_config)
        self.assertListEqual(self.server_dependencies, merged_paths[0][1])

    def test_merge_included_second(self):
        server_config = self._config_id('server')
        redis_config = self._config_id('redis', 'cache')
        merged_paths = merge_dependency_paths([
            (c, self.state_gen.get_dependency_path(c))
            for c in [server_config, redis_config]
        ])
        self.assertEqual(len(merged_paths), 1)
        self.assertEqual(merged_paths[0][0], server_config)
        self.assertListEqual(self.server_dependencies, merged_paths[0][1])

    def test_merge_included_multiple(self):
        sub_svc_config = self._config_id('sub_svc')
        sub_sub_svc_config = self._config_id('sub_sub_svc')
        svc_config = self._config_id('svc')
        server_config = self._config_id('server')
        redis_config = self._config_id('redis', 'queue')
        server2_config = self._config_id('server2')
        merged_paths = merge_dependency_paths([
            (c, self.state_gen.get_dependency_path(c))
            for c in [sub_sub_svc_config, sub_svc_config, svc_config, server_config, redis_config, server2_config]
        ])
        self.assertEqual(len(merged_paths), 2)
        self.assertEqual(merged_paths[0][0], server_config)
        self.assertEqual(merged_paths[1][0], server2_config)
        self.assertListEqual(self.server_dependencies, merged_paths[0][1])
        self.assertListEqual([
            (ITEM_TYPE_CONTAINER, self.map_name, 'svc2', None),
            (ITEM_TYPE_VOLUME, self.map_name, 'server2', 'app_log'),
            (ITEM_TYPE_VOLUME, self.map_name, 'server2', 'server_log'),
        ], merged_paths[1][1])
