# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import logging

from ..action import C_UTIL_ACTION_EXEC_ALL, C_UTIL_ACTION_EXEC_COMMANDS
from ..input import ITEM_TYPE_CONTAINER

log = logging.getLogger(__name__)


class ExecMixin(object):
    """
    Utility mixin for executing configured commands inside containers.
    """
    action_method_names = [
        (ITEM_TYPE_CONTAINER, C_UTIL_ACTION_EXEC_COMMANDS, 'exec_commands'),
        (ITEM_TYPE_CONTAINER, C_UTIL_ACTION_EXEC_ALL, 'exec_container_commands'),
    ]

    def exec_commands(self, action, c_name, run_cmds, **kwargs):
        """
        Runs a single command inside a container.

        :param action: Action configuration.
        :type action: dockermap.map.runner.ActionConfig
        :param c_name: Container name.
        :type c_name: unicode | str
        :param run_cmds: Commands to run.
        :type run_cmds: list[dockermap.map.input.ExecCommand]
        """
        client = action.client
        for run_cmd in run_cmds:
            cmd = run_cmd.cmd
            cmd_user = run_cmd.user
            log.debug("Creating exec command in container %s with user %s: %s.", c_name, cmd_user, cmd)
            ec_kwargs = self.get_exec_create_kwargs(action, c_name, cmd, cmd_user)
            create_result = client.exec_create(**ec_kwargs)
            if create_result:
                e_id = create_result['Id']
                log.debug("Starting exec command with id %s.", e_id)
                es_kwargs = self.get_exec_start_kwargs(action, c_name, e_id)
                client.exec_start(**es_kwargs)
            else:
                log.debug("Exec command was created, but did not return an id. Assuming that it has been started.")

    def exec_container_commands(self, action, c_name, **kwargs):
        """
        Runs all configured commands of a container configuration inside the container instance.

        :param action: Action configuration.
        :type action: dockermap.map.runner.ActionConfig
        :param c_name: Container name.
        :type c_name: unicode | str
        """
        config_cmds = action.config.exec_commands
        if not config_cmds:
            return
        self.exec_commands(action, c_name, run_cmds=config_cmds)
