# Copyright 2015 Mirantis, Inc.
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

from neutron.db import provisioning_blocks

from neutron_lib.api.definitions import portbindings
from neutron_lib.callbacks import registry
from neutron_lib.callbacks import resources
from neutron_lib.callbacks import events
from neutron_lib.plugins.ml2 import api
from neutron.services.trunk import constants
from oslo_log import log as logging

from networking_generic_switch import config as gsw_conf
from networking_generic_switch import devices
from networking_generic_switch.devices import utils as device_utils
from networking_generic_switch import exceptions

LOG = logging.getLogger(__name__)

GENERIC_SWITCH_ENTITY = 'GENERICSWITCH'


class GenericSwitchDriver(api.MechanismDriver):

    def initialize(self):
        """Perform driver initialization.

        Called after all drivers have been loaded and the database has
        been initialized. No abstract methods defined below will be
        called prior to this method being called.
        """

        gsw_devices = gsw_conf.get_devices()
        self.switches = {}
        for switch_info, device_cfg in gsw_devices.items():
            switch = devices.device_manager(device_cfg)
            self.switches[switch_info] = switch
        LOG.info('Devices %s have been loaded', self.switches.keys())
        if not self.switches:
            LOG.error('No devices have been loaded')
        self.warned_del_network = False

        registry.subscribe(
            self.add_subports_to_trunk,
            constants.SUBPORTS,
            events.PRECOMMIT_CREATE
        )

        registry.subscribe(
            self.remove_subports_from_trunk,
            constants.SUBPORTS,
            events.PRECOMMIT_DELETE
        )

    def _check_physnets(self, parent_physnet, physnets):
        for physnet in physnets:
            if physnet != parent_physnet:
                raise Exception("One or more subports have physical networks that do not match that of the parent port")
        return True

    """Processes non-link information relevant to subnet ops.
    """
    def _process_payload(self, payload):
        # Segmentation ids of the subports already bound in the trunk
        trunk_details = payload.parent_port.get('trunk_details')
        current_seg_ids = [
            sub_port['segmentation_id'] for sub_port in
            trunk_details["sub_ports"]
        ]

        # The segmentation id used as the native vlan; the one corresponding to the
        # provider network
        native_vlan = payload.network['provider:segmentation_id']

        return current_seg_ids, native_vlan

    def remove_subports_from_trunk(self, resource, event, plugin, payload):
        if self._is_port_bound(payload.parent_port):
            # seg_ids to remove from switches
            to_remove_seg_ids = []
            for subport in payload.subports:
                # No physnet or seg_id checks on removal, since those are not
                # terribly relevant on removal - if, for some reason, physnet
                # or seg id didn't match, removing the port fixes a
                # misconfiguration, which is desirable
                seg_id = subport.get('segmentation_id')
                to_remove_seg_ids.append(seg_id)

            # Retrieve switch devices and the port which should be configured
            # on them
            switches, port_id = self._process_link_information(payload)

            current_seg_ids, native_vlan = self._process_payload(payload)

            for switch in switches:
                if switch.get_trunk_mode() == 'dynamic':
                    switch.remove_trunk_vlans(to_remove_seg_ids, port_id, current_seg_ids, native_vlan)

    def add_subports_to_trunk(self, resource, event, plugin, payload):
        if self._is_port_bound(payload.parent_port):
            # Goes through the subports being added to the trunk object
            # and collects them in an array if valid
            parent_physnet = payload.network['provider:physical_network']
            to_add_seg_ids = []
            for subport in payload.subports:
                subport_id = subport['port_id']

                # Every subport must belong to the same physical network as the
                # Trunk parent port
                physnet = payload.physnets[subport_id]
                if physnet != parent_physnet:
                    raise exceptions.BadPhysnetsAddError(subport_id=subport_id,
                        physnet=physnet, parent_physnet=parent_physnet)

                # Checks if the segmentation id assigned to the port by the
                # user matches it's networks segmentation id (inherit); if
                # an arbitrary value was accepted blindly, any network could
                # be broken into!
                network_seg_id = payload.seg_ids[subport_id]
                seg_id = subport.get('segmentation_id')
                if seg_id != network_seg_id:
                    raise exceptions.BadSegIdAddError(subport_id=subport_id,
                        actual=seg_id, parent=network_seg_id)

                # If all checks pass, the seg id is valid and may be added
                to_add_seg_ids.append(seg_id)

            # Retrieve switch devices and the port which should be configured
            # on them
            switches, port_id = self._process_link_information(payload)

            current_seg_ids, native_vlan = self._process_payload(payload)

            # Execute switch configuration
            for switch in switches:
                if switch.get_trunk_mode() == 'dynamic':
                    switch.add_trunk_vlans(
                        to_add_seg_ids, port_id, current_seg_ids, native_vlan
                    )

    # Processes a TrunkPayload object to find the correct switch devices and
    # switch port id
    def _process_link_information(self, payload):
        parent_port = payload.parent_port
        binding_profile = parent_port.get('binding:profile')
        local_link_information = binding_profile.get('local_link_information')

        # transcribes LAG information if parent port is bonded
        local_group_information = binding_profile.get('local_group_information')
        if local_group_information:
            self._lag_alter_local_link(local_group_information, local_link_information)

        switch_devices = []
        # The switch port id of the port which should be set:
        # e.g.: xe-0/0/13, ae-0/0/11 etc...
        port_id = None
        for local_link in local_link_information:

            # The port id has to be identical for all link definitions in a
            # bond, non-bonded ports shouldn't have more than one link
            if port_id is None:
                port_id = local_link.get('port_id')
            elif port_id != local_link.get('port_id'):
                LOG.warning(
                    "Multiple links have been detected with mismatching "
                    "router port ids; only bonds are expected to have more "
                    "than one link WITH matching port ids. Setup will "
                    "continue, but may behave strangely!")

            # retrieve the correct switch for the given link
            switch_info = local_link.get('switch_info')
            switch_id = local_link.get('switch_id')
            switch = device_utils.get_switch_device(
                self.switches,
                switch_info=switch_info,
                ngs_mac_address=switch_id
            )
            switch_devices.append(switch)

        return switch_devices, port_id


    def create_network_precommit(self, context):
        """Allocate resources for a new network.

        :param context: NetworkContext instance describing the new
        network.

        Create a new network, allocating resources as necessary in the
        database. Called inside transaction context on session. Call
        cannot block.  Raising an exception will result in a rollback
        of the current transaction.
        """
        pass

    def create_network_postcommit(self, context):
        """Create a network.

        :param context: NetworkContext instance describing the new
        network.

        Called after the transaction commits. Call can block, though
        will block the entire process so care should be taken to not
        drastically affect performance. Raising an exception will
        cause the deletion of the resource.
        """

        network = context.current
        network_id = network['id']
        provider_type = network['provider:network_type']
        segmentation_id = network['provider:segmentation_id']
        physnet = network['provider:physical_network']

        if provider_type == 'vlan' and segmentation_id:
            # Create vlan on all switches from this driver
            for switch_name, switch in self._get_devices_by_physnet(physnet):
                try:
                    switch.add_network(segmentation_id, network_id)
                except Exception as e:
                    LOG.error("Failed to create network %(net_id)s "
                              "on device: %(switch)s, reason: %(exc)s",
                              {'net_id': network_id,
                               'switch': switch_name,
                               'exc': e})
                else:
                    LOG.info('Network %(net_id)s has been added on device '
                             '%(device)s', {'net_id': network['id'],
                                            'device': switch_name})

    def update_network_precommit(self, context):
        """Update resources of a network.

        :param context: NetworkContext instance describing the new
        state of the network, as well as the original state prior
        to the update_network call.

        Update values of a network, updating the associated resources
        in the database. Called inside transaction context on session.
        Raising an exception will result in rollback of the
        transaction.

        update_network_precommit is called for all changes to the
        network state. It is up to the mechanism driver to ignore
        state or state changes that it does not know or care about.
        """
        pass

    def update_network_postcommit(self, context):
        """Update a network.

        :param context: NetworkContext instance describing the new
        state of the network, as well as the original state prior
        to the update_network call.

        Called after the transaction commits. Call can block, though
        will block the entire process so care should be taken to not
        drastically affect performance. Raising an exception will
        cause the deletion of the resource.

        update_network_postcommit is called for all changes to the
        network state.  It is up to the mechanism driver to ignore
        state or state changes that it does not know or care about.
        """
        pass

    def delete_network_precommit(self, context):
        """Delete resources for a network.

        :param context: NetworkContext instance describing the current
        state of the network, prior to the call to delete it.

        Delete network resources previously allocated by this
        mechanism driver for a network. Called inside transaction
        context on session. Runtime errors are not expected, but
        raising an exception will result in rollback of the
        transaction.
        """
        pass

    def delete_network_postcommit(self, context):
        """Delete a network.

        :param context: NetworkContext instance describing the current
        state of the network, prior to the call to delete it.

        Called after the transaction commits. Call can block, though
        will block the entire process so care should be taken to not
        drastically affect performance. Runtime errors are not
        expected, and will not prevent the resource from being
        deleted.
        """
        network = context.current
        provider_type = network['provider:network_type']
        segmentation_id = network['provider:segmentation_id']
        physnet = network['provider:physical_network']

        if provider_type == 'vlan' and segmentation_id:
            # Delete vlan on all switches from this driver
            for switch_name, switch in self._get_devices_by_physnet(physnet):
                try:
                    # NOTE(mgoddard): The del_network method was modified to
                    # accept the network ID. The switch object may still be
                    # implementing the old interface, so retry on a TypeError.
                    try:
                        switch.del_network(segmentation_id, network['id'])
                    except TypeError:
                        if not self.warned_del_network:
                            msg = (
                                'The del_network device method should accept '
                                'the network ID. Falling back to just the '
                                'segmentation ID for %(device)s. This '
                                'transitional support will be removed in the '
                                'Rocky release')
                            LOG.warn(msg, {'device': switch_name})
                            self.warned_del_network = True
                        switch.del_network(segmentation_id)
                except Exception as e:
                    LOG.error("Failed to delete network %(net_id)s "
                              "on device: %(switch)s, reason: %(exc)s",
                              {'net_id': network['id'],
                               'switch': switch_name,
                               'exc': e})
                else:
                    LOG.info('Network %(net_id)s has been deleted on device '
                             '%(device)s', {'net_id': network['id'],
                                            'device': switch_name})

    def create_subnet_precommit(self, context):
        """Allocate resources for a new subnet.

        :param context: SubnetContext instance describing the new
        subnet.
        rt = context.current
        device_id = port['device_id']
        device_owner = port['device_owner']
        Create a new subnet, allocating resources as necessary in the
        database. Called inside transaction context on session. Call
        cannot block.  Raising an exception will result in a rollback
        of the current transaction.
        """
        pass

    def create_subnet_postcommit(self, context):
        """Create a subnet.

        :param context: SubnetContext instance describing the new
        subnet.

        Called after the transaction commits. Call can block, though
        will block the entire process so care should be taken to not
        drastically affect performance. Raising an exception will
        cause the deletion of the resource.
        """
        pass

    def update_subnet_precommit(self, context):
        """Update resources of a subnet.

        :param context: SubnetContext instance describing the new
        state of the subnet, as well as the original state prior
        to the update_subnet call.

        Update values of a subnet, updating the associated resources
        in the database. Called inside transaction context on session.
        Raising an exception will result in rollback of the
        transaction.

        update_subnet_precommit is called for all changes to the
        subnet state. It is up to the mechanism driver to ignore
        state or state changes that it does not know or care about.
        """
        pass

    def update_subnet_postcommit(self, context):
        """Update a subnet.

        :param context: SubnetContext instance describing the new
        state of the subnet, as well as the original state prior
        to the update_subnet call.

        Called after the transaction commits. Call can block, though
        will block the entire process so care should be taken to not
        drastically affect performance. Raising an exception will
        cause the deletion of the resource.

        update_subnet_postcommit is called for all changes to the
        subnet state.  It is up to the mechanism driver to ignore
        state or state changes that it does not know or care about.
        """
        pass

    def delete_subnet_precommit(self, context):
        """Delete resources for a subnet.

        :param context: SubnetContext instance describing the current
        state of the subnet, prior to the call to delete it.

        Delete subnet resources previously allocated by this
        mechanism driver for a subnet. Called inside transaction
        context on session. Runtime errors are not expected, but
        raising an exception will result in rollback of the
        transaction.
        """
        pass

    def delete_subnet_postcommit(self, context):
        """Delete a subnet.

        :param context: SubnetContext instance describing the current
        state of the subnet, prior to the call to delete it.

        Called after the transaction commits. Call can block, though
        will block the entire process so care should be taken to not
        drastically affect performance. Runtime errors are not
        expected, and will not prevent the resource from being
        deleted.
        """
        pass

    def create_port_precommit(self, context):
        """Allocate resources for a new port.

        :param context: PortContext instance describing the port.

        Create a new port, allocating resources as necessary in the
        database. Called inside transaction context on session. Call
        cannot block.  Raising an exception will result in a rollback
        of the current transaction.
        """
        pass

    def create_port_postcommit(self, context):
        """Create a port.

        :param context: PortContext instance describing the port.

        Called after the transaction completes. Call can block, though
        will block the entire process so care should be taken to not
        drastically affect performance.  Raising an exception will
        result in the deletion of the resource.
        """
        pass

    def update_port_precommit(self, context):
        """Update resources of a port.

        :param context: PortContext instance describing the new
        state of the port, as well as the original state prior
        to the update_port call.

        Called inside transaction context on session to complete a
        port update as defined by this mechanism driver. Raising an
        exception will result in rollback of the transaction.

        update_port_precommit is called for all changes to the port
        state. It is up to the mechanism driver to ignore state or
        state changes that it does not know or care about.
        """
        pass

    def update_port_postcommit(self, context):
        """Update a port.

        :param context: PortContext instance describing the new
        state of the port, as well as the original state prior
        to the update_port call.

        Called after the transaction completes. Call can block, though
        will block the entire process so care should be taken to not
        drastically affect performance.  Raising an exception will
        result in the deletion of the resource.

        update_port_postcommit is called for all changes to the port
        state. It is up to the mechanism driver to ignore state or
        state changes that it does not know or care about.
        """
        port = context.current
        if self._is_port_bound(port):
            binding_profile = port['binding:profile']
            local_link_information = binding_profile.get(
                'local_link_information')
            local_group_information = binding_profile.get(
                'local_group_information')
            if not local_link_information:
                return
            if local_group_information:
                self._lag_alter_local_link(local_group_information,
                                           local_link_information)
            for switch in local_link_information:
                switch_info = switch.get('switch_info')
                switch_id = switch.get('switch_id')
                switch_device = device_utils.get_switch_device(
                    self.switches, switch_info=switch_info,
                    ngs_mac_address=switch_id)
                if not switch_device:
                    return
                provisioning_blocks.provisioning_complete(
                    context._plugin_context, port['id'], resources.PORT,
                    GENERIC_SWITCH_ENTITY)
        elif self._is_port_bound(context.original):
            # The port has been unbound. This will cause the local link
            # information to be lost, so remove the port from the network on
            # the switch now while we have the required information.
            self._unplug_port_from_network(context.original,
                                           context.network.current)

    def delete_port_precommit(self, context):
        """Delete resources of a port.

        :param context: PortContext instance describing the current
        state of the port, prior to the call to delete it.

        Called inside transaction context on session. Runtime errors
        are not expected, but raising an exception will result in
        rollback of the transaction.
        """
        pass

    def delete_port_postcommit(self, context):
        """Delete a port.

        :param context: PortContext instance describing the current
        state of the port, prior to the call to delete it.

        Called after the transaction completes. Call can block, though
        will block the entire process so care should be taken to not
        drastically affect performance.  Runtime errors are not
        expected, and will not prevent the resource from being
        deleted.
        """

        port = context.current
        if self._is_port_bound(port):
            self._unplug_port_from_network(port, context.network.current)

    def bind_port(self, context):
        """Attempt to bind a port.

        :param context: PortContext instance describing the port

        This method is called outside any transaction to attempt to
        establish a port binding using this mechanism driver. Bindings
        may be created at each of multiple levels of a hierarchical
        network, and are established from the top level downward. At
        each level, the mechanism driver determines whether it can
        bind to any of the network segments in the
        context.segments_to_bind property, based on the value of the
        context.host property, any relevant port or network
        attributes, and its own knowledge of the network topology. At
        the top level, context.segments_to_bind contains the static
        segments of the port's network. At each lower level of
        binding, it contains static or dynamic segments supplied by
        the driver that bound at the level above. If the driver is
        able to complete the binding of the port to any segment in
        context.segments_to_bind, it must call context.set_binding
        with the binding details. If it can partially bind the port,
        it must call context.continue_binding with the network
        segments to be used to bind at the next lower level.

        If the binding results are committed after bind_port returns,
        they will be seen by all mechanism drivers as
        update_port_precommit and update_port_postcommit calls. But if
        some other thread or process concurrently binds or updates the
        port, these binding results will not be committed, and
        update_port_precommit and update_port_postcommit will not be
        called on the mechanism drivers with these results. Because
        binding results can be discarded rather than committed,
        drivers should avoid making persistent state changes in
        bind_port, or else must ensure that such state changes are
        eventually cleaned up.

        Implementing this method explicitly declares the mechanism
        driver as having the intention to bind ports. This is inspected
        by the QoS service to identify the available QoS rules you
        can use with ports.
        """

        port = context.current
        binding_profile = port['binding:profile']
        local_link_information = binding_profile.get('local_link_information')
        local_group_information = binding_profile.get('local_group_information')
        if self._is_port_supported(port) and local_link_information:
            if local_group_information:
                self._setup_lag(local_group_information,
                                local_link_information)
            else:
                # TODO(janvondra): Consider check whether there is only one switch in local_link_info in case of no local_group_information
                pass
            #TODO(janvondra): Some switches are able to copy configuration thus no need to perform following for cycle
            for switch in local_link_information:
                self._bind_port_to_switch(context, port, switch)

    def _bind_port_to_switch(self, context, port, switch):
        switch_info = switch.get('switch_info')
        switch_id = switch.get('switch_id')
        switch_device = device_utils.get_switch_device(
            self.switches, switch_info=switch_info,
            ngs_mac_address=switch_id)
        if not switch:
            return
        network = context.network.current

        net_type = network['provider:network_type']
        if net_type != 'vlan':
            raise exceptions.BadNetTypeError(actual=net_type)

        physnet = network['provider:physical_network']
        switch_physnets = switch_device._get_physical_networks()
        if switch_physnets and physnet not in switch_physnets:
            LOG.error(
                "Cannot bind port %(port)s as device %(device)s is "
                "not on physical network %(physnet)",
                {'port_id': port['id'], 'device': switch_info,
                 'physnet': physnet})
            return
        port_id = switch.get('port_id')
        segments = context.segments_to_bind
        # If segmentation ID is None, set vlan 1
        segmentation_id = segments[0].get('segmentation_id') or '1'
        provisioning_blocks.add_provisioning_component(
            context._plugin_context, port['id'], resources.PORT,
            GENERIC_SWITCH_ENTITY)
        LOG.debug("Putting port {port} on {switch_info} to vlan: "
                  "{segmentation_id}".format(
                    port=port_id,
                    switch_info=switch_info,
                    segmentation_id=segmentation_id))

        if self._is_trunk(port) and switch_device.get_trunk_mode() == 'dynamic':
            trunk_details = port['trunk_details']
            self._setup_trunk(switch_device, network, trunk_details, port_id)
        else:
            # Move port to network
            switch_device.plug_port_to_network(port_id, segmentation_id)
            LOG.info("Successfully bound port %(port_id)s in segment "
                     "%(segment_id)s on device %(device)s",
                     {'port_id': port['id'],
                      'device': switch_info,
                      'segment_id': segmentation_id})

        context.set_binding(segments[0][api.ID],
                            portbindings.VIF_TYPE_OTHER, {})

    def _check_subport_seg_ids(self, subports):
        for subport in subports:
            if subport.get('provider:segmentation_id') != subport.get('segmentation_id'):
                raise exceptions.BadSegIdBindError(
                    actual=subport.get('segmentation_id'),
                    parent=subport.get('provider:segmentation_id')
                )

    def _setup_trunk(self, switch, network, trunk_details, port_id):
        self._check_subport_seg_ids(trunk_details.get("sub_ports"))

        native_vlan = network['provider:segmentation_id']
        parent_physnet = network['provider:physical_network']

        physnets = [sub_port['provider:physical_network'] for sub_port in trunk_details["sub_ports"]]
        self._check_physnets(parent_physnet, physnets)

        seg_ids = [sub_port['segmentation_id'] for sub_port in trunk_details["sub_ports"]]
        switch.setup_trunk(native_vlan, seg_ids, port_id)

    @staticmethod
    def _is_trunk(port):
        return 'trunk_details' in port

    def _setup_lag(self, local_group, local_link):
        """Setup line aggregation on switch and alter local_link
        according to aggregation setup

        Given the information in local_link and local_group either
        multi-chassis or single-chassis line aggregation is configured

        :param local_group: local group dictionary
        :param local_link: local link dictionary
        :return interface designation (string)
        """
        self._lag_alter_local_link(local_group, local_link)
        if len(local_link) > 1:
            self._setup_mc_lag(local_group, local_link)
        else:
            self._setup_sc_lag(local_group, local_link)

    def _lag_alter_local_link(self, local_group, local_link):
        agg_interface = self._get_lag_interface(local_group)
        if agg_interface:
            for switch in local_link:
                switch['port_id'] = agg_interface
        else:
            #TODO(janvondra): raise sane error
            pass

    def _setup_mc_lag(self, local_group, local_link):
        """Setup multi-chassis link aggregation

        :param local_group: local group dictionary
        :param local_link: local link dictionary
        """
        pass

    def _setup_sc_lag(self, local_group, local_link):
        pass

    @staticmethod
    def _get_lag_interface(local_group):
        """Return interface designation according to the
        port group property interface_name

        :param local_group: local_group dictionary
        :return: interface designation (string)
        """
        bond_properties = local_group.get("bond_properties")
        return bond_properties.get("bond_interface_name")

    def _get_lag_id(self, local_group):
        pass

    def _get_used_lag_interfaces(self, local_link):
        lag_interfaces = []
        for router in local_link:
            # get configured lag interfaces and add them to interfaces
            pass
        return lag_interfaces

    @staticmethod
    def _is_port_supported(port):
        """Return whether a port is supported by this driver.

        Ports supported by this driver have a VNIC type of 'baremetal'.

        :param port: The port to check
        :returns: Whether the port is supported by the NGS driver
        """
        vnic_type = port[portbindings.VNIC_TYPE]
        return vnic_type == portbindings.VNIC_BAREMETAL

    @staticmethod
    def _is_port_bound(port):
        """Return whether a port is bound by this driver.

        Ports bound by this driver have their VIF type set to 'other'.

        :param port: The port to check
        :returns: Whether the port is bound by the NGS driver
        """
        if not GenericSwitchDriver._is_port_supported(port):
            return False

        vif_type = port[portbindings.VIF_TYPE]
        return vif_type == portbindings.VIF_TYPE_OTHER

    def _unplug_port_from_network(self, port, network):
        """Unplug a port from a network.

        If the configuration required to unplug the port is not present
        (e.g. local link information), the port will not be unplugged and no
        exception will be raised.

        :param port: The port to unplug
        :param network: The network from which to unplug the port
        """
        binding_profile = port['binding:profile']
        local_link_information = binding_profile.get('local_link_information')
        local_group_information = binding_profile.get(
            'local_group_information')
        if not local_link_information:
            return
        if local_group_information:
            self._lag_alter_local_link(local_group_information,
                                       local_link_information)
        for switch in local_link_information:
            switch_info = switch.get('switch_info')
            switch_id = switch.get('switch_id')
            switch_device = device_utils.get_switch_device(
                self.switches, switch_info=switch_info,
                ngs_mac_address=switch_id)
            if not switch_device:
                return
            port_id = local_link_information[0].get('port_id')
            # If segmentation ID is None, set vlan 1
            segmentation_id = network.get('provider:segmentation_id') or '1'
            LOG.debug("Unplugging port {port} on {switch_info} from vlan: "
                      "{segmentation_id}".format(
                          port=port_id,
                          switch_info=switch_info,
                          segmentation_id=segmentation_id))
            try:
                if self._is_trunk(port) and switch_device.get_trunk_mode() == "dynamic":
                    switch_device.unset_trunk(port_id, segmentation_id)
                else:
                    switch_device.delete_port(port_id, segmentation_id)
            except Exception as e:
                LOG.error("Failed to unplug port %(port_id)s "
                          "on device: %(switch)s from network %(net_id)s "
                          "reason: %(exc)s",
                          {'port_id': port['id'], 'net_id': network['id'],
                           'switch': switch_info, 'exc': e})
                raise e
        LOG.info('Port %(port_id)s has been unplugged from network '
                 '%(net_id)s on device %(device)s',
                 {'port_id': port['id'], 'net_id': network['id'],
                  'device': switch_info})

    def _get_devices_by_physnet(self, physnet):
        """Generator yielding switches on a particular physical network.

        :param physnet: Physical network to filter by.
        :returns: Yields 2-tuples containing the name of the switch and the
            switch device object.
        """
        for switch_name, switch in self.switches.items():
            physnets = switch._get_physical_networks()
            # NOTE(mgoddard): If the switch has no physical networks then
            # follow the old behaviour of mapping all networks to it.
            if not physnets or physnet in physnets:
                yield switch_name, switch
