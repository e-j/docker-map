# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import logging
import signal

from requests.exceptions import Timeout

from ..action import C_UTIL_ACTION_SIGNAL_STOP
from ..input import ITEM_TYPE_CONTAINER

log = logging.getLogger(__name__)


class SignalMixin(object):
    action_method_names = [
        (ITEM_TYPE_CONTAINER, C_UTIL_ACTION_SIGNAL_STOP, 'signal_stop'),
    ]

    def signal_stop(self, action, c_name, **kwargs):
        """
        Stops a container, either using the default client stop method, or sending a custom signal and waiting
        for the container to stop.

        :param action: Action configuration.
        :type action: dockermap.map.runner.ActionConfig
        :param c_name: Container name.
        :type c_name: unicode | str
        :param kwargs: Additional keyword arguments to complement or override the configuration-based values.
        :type kwargs: dict
        """
        client = action.client
        sig = action.config.stop_signal
        stop_kwargs = self.get_stop_kwargs(action, c_name, kwargs=kwargs)
        if not sig or sig == 'SIGTERM' or sig == signal.SIGTERM:
            try:
                client.stop(**stop_kwargs)
            except Timeout:
                log.warning("Container did not stop in time - sent SIGKILL.")
        else:
            log.debug("Sending signal %s to the container %s and waiting for stop.", sig, c_name)
            client.kill(c_name, signal=sig)
            client.wait(c_name, timeout=stop_kwargs.get('timeout', 10))
