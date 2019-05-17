# Copyright 2016 Mirantis, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from neutron_lib import exceptions
from neutron.plugins.ml2.common import exceptions as plugin_exceptions

from networking_generic_switch._i18n import _


class GenericSwitchException(exceptions.NeutronException):
    message = _("%(method)s failed.")


class GenericSwitchConfigException(exceptions.NeutronException):
    message = _("%(option)s must be one of: %(allowed_options)s")


class GenericSwitchEntrypointLoadError(GenericSwitchException):
    message = _("Failed to load entrypoint %(ep)s: %(err)s")


class GenericSwitchNetmikoMethodError(GenericSwitchException):
    message = _("Can not parse arguments: commands %(cmds)s, args %(args)s")


class GenericSwitchNetmikoNotSupported(GenericSwitchException):
    message = _("Netmiko does not support device type %(device_type)s")


class GenericSwitchNetmikoConnectError(GenericSwitchException):
    message = _("Netmiko connection error: %(config)s, error: %(error)s")


class GenericSwitchNetmikoConfigError(GenericSwitchException):
    message = _("Netmiko configuration error: %(config)s, error: %(error)s")


class BadSegIdAddError(plugin_exceptions.MechanismDriverCriticalError):
    message = _("Attempted to add subport [%(subport_id)s] to trunk with "
                "segmentation id not matching that of parent network: "
                "was %(actual)s, expected %(parent)s")


class BadSegIdBindError(plugin_exceptions.MechanismDriverCriticalError):
    message = _("Attempted to bind trunk with segmentation id not matching "
                "that of parent network: was %(actual)s, expected %(parent)s")


class BadPhysnetsAddError(plugin_exceptions.MechanismDriverCriticalError):
    message = _("Attempted to add subport [%(subport_id)s] to trunk with "
                "physnet not matching that of parent network: "
                "was %(physnet)s, expected %(parent_physnet)s")

class BadNetTypeError(plugin_exceptions.MechanismDriverCriticalError):
    message = _("Only networks of type 'vlan' are allowed; was %(actual)s")
