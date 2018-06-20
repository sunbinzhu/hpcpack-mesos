import threading
from collections import namedtuple
from datetime import datetime, timedelta

from typing import Iterable, Callable

import logging_aux
from restclient import HpcRestClient


class HpcClusterManager(object):
    CHECK_CONFIGURING_NODES_INTERVAL = 5  # in seconds
    MESOS_NODE_GROUP_NAME = "Mesos"
    MESOS_NODE_GROUP_DESCRIPTION = "The Mesos compute nodes in the cluster"

    NODE_IDLE_TIMEOUT = 180.0

    # TODO: add configuration_timeout
    def __init__(self, hpc_rest_client, provisioning_timeout=timedelta(minutes=15),
                 heartbeat_timeout=timedelta(minutes=3), node_group=""):
        # type: (HpcRestClient, timedelta, timedelta, str) -> ()
        self._heart_beat_table = {}  # type: dict[str, SlaveInfo]
        self._removed_nodes = set()  # type: set[str]
        self._node_idle_check_table = {}
        self.logger = logging_aux.init_logger_aux("hpcframework.clustermanager", "hpcframework.clustermanager.log")
        self._table_lock = threading.Lock()
        self._provisioning_timeout = provisioning_timeout
        self._heartbeat_timeout = heartbeat_timeout
        self._hpc_client = hpc_rest_client
        self._node_group = node_group  # TODO: change to a centralized config

        self._node_idle_timedelta = timedelta(0, self.NODE_IDLE_TIMEOUT)

        # callbacks
        self._node_closed_callbacks = []  # type: list[Callable[[list[str]], ()]]

    def __get_hostname_from_fqdn(self, fqdn):
        return fqdn.split('.')[0]

    def _node_group_specified(self):
        return self._node_group != ""

    def subscribe_node_closed_callback(self, callback):
        # type: (Callable[[list[str]], ()]) -> ()
        self._node_closed_callbacks.append(callback)

    def add_slaveinfo(self, fqdn, agent_id, task_id, cpus, last_heartbeat=datetime.utcnow()):
        u_fqdn = fqdn.upper()
        hostname = self.__get_hostname_from_fqdn(u_fqdn)
        if hostname in self._heart_beat_table:
            if self._heart_beat_table[hostname].fqdn != u_fqdn:
                self.logger.error(
                    "Duplicated hostname {} detected. Existing fqdn: {}, new fqdn {}. Ignore new heartbeat entry.".format(
                        hostname, self._heart_beat_table[hostname].fqdn, u_fqdn))
                return
            elif self._heart_beat_table[hostname].state != HpcState.Closed:
                self.logger.warn("Heart beat entry of {} existed. old value: {}.".format(
                    hostname, str(self._heart_beat_table[hostname])))
        slaveinfo = SlaveInfo(hostname, u_fqdn, agent_id, task_id, cpus, last_heartbeat, HpcState.Provisioning)
        self._heart_beat_table[hostname] = slaveinfo
        self.logger.info("Heart beat entry added: {}".format(str(slaveinfo)))

    def on_slave_heartbeat(self, hostname, now=datetime.utcnow()):
        # type: (str, datetime) -> ()
        u_hostname = hostname.upper()
        if u_hostname in self._heart_beat_table:
            self._heart_beat_table[u_hostname] = self._heart_beat_table[u_hostname]._replace(last_heartbeat=now)
            self.logger.info("Heatbeat from host {}".format(u_hostname))
            if self._heart_beat_table[u_hostname].state == HpcState.Provisioning:
                with self._table_lock:
                    if self._heart_beat_table[u_hostname].state == HpcState.Provisioning:
                        self._heart_beat_table[u_hostname] = self._heart_beat_table[u_hostname]._replace(
                            state=HpcState.Configuring)
                        self.logger.info("Configuring Host {}".format(u_hostname))
        else:
            self.logger.error("Host {} is not recognized. Heartbeat ignored.".format(u_hostname))
            self.logger.debug("_table {} ".format(self._heart_beat_table))

    def get_task_info(self, hostname):
        u_hostname = hostname.upper()
        if u_hostname in self._heart_beat_table:
            entry = self._heart_beat_table[u_hostname]
            return entry.task_id, entry.agent_id
        else:
            self.logger.error("Host {} is not recognized. Failed to get task info.".format(u_hostname))
            return "", ""

    def get_host_state(self, hostname):
        u_hostname = hostname.upper()
        if u_hostname in self._heart_beat_table:
            entry = self._heart_beat_table[u_hostname]
            return entry.state
        else:
            self.logger.error("Host {} is not recognized. Failed to get host state.".format(u_hostname))
            return HpcState.Unknown

    def _exec_callback(self, callbacks):
        for callback in callbacks:
            try:
                self.logger.debug('Callback %s on %s' % (callback.__name__))
                callback()
            except Exception as e:
                self.logger.exception('Error in %s callback: %s' % (callback.__name__, str(e)))

    def check_fqdn_collision(self, fqdn):
        u_fqdn = fqdn.upper()
        hostname = self.__get_hostname_from_fqdn(u_fqdn)
        if hostname in self._heart_beat_table:
            if self._heart_beat_table[hostname].fqdn != u_fqdn:
                return True
        return False

    def check_timeout(self, now=datetime.utcnow()):
        provision_timeout_list = []
        heartbeat_timeout_list = []
        running_list = []
        for host in dict(self._heart_beat_table).itervalues():
            if host.state == HpcState.Provisioning and now - host.last_heartbeat >= self._provisioning_timeout:
                self.logger.warn("Provisioning timeout: {}".format(str(host)))
                provision_timeout_list.append(host)
            elif host.state == HpcState.Running:
                if now - host.last_heartbeat >= self._heartbeat_timeout:
                    self.logger.warn("Heartbeat lost: {}".format(str(host)))
                    heartbeat_timeout_list.append(host)
                else:
                    running_list.append(host)
        return (provision_timeout_list, heartbeat_timeout_list, running_list)

    def get_cores_in_provisioning(self):
        cores = 0.0
        for host in dict(self._heart_beat_table).itervalues():
            if host.state == HpcState.Provisioning or host.state == HpcState.Configuring:
                cores += host.cpus
        self.logger.info("Cores in provisioning: {}".format(cores))
        return cores

    def _configure_compute_nodes(self):
        configuring_node_names = []
        configured_node_names = []
        for host in dict(self._heart_beat_table).itervalues():
            if host.state == HpcState.Configuring:
                configuring_node_names.append(host.hostname)
        if configuring_node_names:
            self.logger.info("Nodes in configuring: {}".format(configuring_node_names))
            configured_node_names = self._configure_compute_nodes_state_machine(configuring_node_names)
        if configured_node_names:
            self.logger.info("Nodes configured: {}".format(configured_node_names))
            self._set_nodes_running(configured_node_names)

    def _configure_compute_nodes_state_machine(self, configuring_node_names):
        if not configuring_node_names:
            return []

        groups = self._hpc_client.list_node_groups(self.MESOS_NODE_GROUP_NAME)
        if self.MESOS_NODE_GROUP_NAME not in groups:
            self._hpc_client.add_node_group(self.MESOS_NODE_GROUP_NAME, self.MESOS_NODE_GROUP_DESCRIPTION)

        # We won't create target node group, but check if it exists
        # Check after Mesos group has been created to support specified group is Mesos group
        if self._node_group_specified():
            target_group = self._hpc_client.list_node_groups(self._node_group)
            if self._node_group.upper() not in (x.upper() for x in target_group):
                self.logger.error(
                    "Target node group is not created:{}. Stop configure compute nodes.".format(self._node_group))
                return []

        node_status_list = self._hpc_client.get_node_status_exact(configuring_node_names)
        self.logger.info("Get node_status_list:{}".format(str(node_status_list)))
        unapproved_node_list = []
        take_offline_node_list = []
        bring_online_node_list = []
        change_node_group_node_list = []
        configured_node_names = []
        invalid_state_node_dict = {}
        for node_status in node_status_list:
            node_name = node_status[HpcRestClient.NODE_STATUS_NODE_NAME_KEY]
            node_state = node_status[HpcRestClient.NODE_STATUS_NODE_STATE_KEY]
            if self._check_node_health_unapproved(node_status):
                unapproved_node_list.append(node_name)
            # node approved
            elif (self._check_node_in_mesos_group(node_status) or (
                    self._node_group_specified() and self._check_node_in_specified_group(node_status))):
                if self._check_node_state_online(node_status):
                    take_offline_node_list.append(node_name)
                elif self._check_node_state_offline(node_status):
                    change_node_group_node_list.append(node_name)
                else:
                    invalid_state_node_dict[node_name] = node_state
            # node group properly set
            elif self._check_node_state_offline(node_status):
                bring_online_node_list.append(node_name)
            elif self._check_node_state_online(node_status):
                # this node is all set
                configured_node_names.append(node_name)
            else:
                invalid_state_node_dict[node_name] = node_state
        try:
            if invalid_state_node_dict:
                self.logger.info("Node(s) in invalid state when configuring: {}".format(str(invalid_state_node_dict)))
            if unapproved_node_list:
                self.logger.info("Assigning node template for node(s): {}".format(str(unapproved_node_list)))
                self._hpc_client.assign_default_compute_node_template(unapproved_node_list)
            if take_offline_node_list:
                self.logger.info("Taking node(s) offline: {}".format(str(take_offline_node_list)))
                self._hpc_client.take_nodes_offline(take_offline_node_list)
            if bring_online_node_list:
                self.logger.info("Bringing node(s) online: {}".format(str(bring_online_node_list)))
                self._hpc_client.bring_nodes_online(bring_online_node_list)
            if change_node_group_node_list:
                self.logger.info("Changing node group node(s): {}".format(str(change_node_group_node_list)))
                self._hpc_client.add_node_to_node_group(self.MESOS_NODE_GROUP_NAME, change_node_group_node_list)
                if self._node_group_specified():
                    self._hpc_client.add_node_to_node_group(self._node_group, change_node_group_node_list)
        except:
            # Swallow all exceptions here. As we don't want any exception to prevent configured nodes to work
            self.logger.exception('Exception happened when configuring compute node.')

        return configured_node_names

    def _check_runaway_and_idle_compute_nodes(self):
        (provision_timeout_list, heartbeat_timeout_list, running_list) = self.check_timeout()
        if provision_timeout_list:
            self.logger.info("Get provision_timeout_list:{}".format(str(provision_timeout_list)))
            self._set_nodes_draining(host.hostname for host in provision_timeout_list)
        if heartbeat_timeout_list:
            self.logger.info("Get heartbeat_timeout_list:{}".format(str(heartbeat_timeout_list)))
            self._set_nodes_draining(host.hostname for host in heartbeat_timeout_list)
        if running_list:
            running_node_names = [host.hostname for host in running_list]
            idle_nodes = self._hpc_client.check_nodes_idle(running_node_names)
            self.logger.info("Get idle_nodes:{}".format(str(idle_nodes)))
            idle_timeout_nodes = self._check_node_idle_timeout([node.node_name for node in idle_nodes])
            self.logger.info("Get idle_timeout_nodes:{}".format(str(idle_timeout_nodes)))
            self._set_nodes_draining(idle_timeout_nodes)

    def _check_node_idle_timeout(self, node_names):
        new_node_idle_check_table = {}
        for u_node_name in self._upper_strings(node_names):
            if u_node_name in self._node_idle_check_table:
                if u_node_name in self._removed_nodes:
                    new_node_idle_check_table[u_node_name] = datetime.now()
                    self._removed_nodes.discard(u_node_name)
                else:
                    new_node_idle_check_table[u_node_name] = self._node_idle_check_table[u_node_name]
            else:
                new_node_idle_check_table[u_node_name] = datetime.now()
        self._node_idle_check_table = new_node_idle_check_table
        now = datetime.now()
        self.logger.info("_check_node_idle_timeout: " + str(self._node_idle_check_table))
        return [name for name, value in self._node_idle_check_table.iteritems() if
                (now - value) > self._node_idle_timedelta]

    def start_configure_cluster_timer(self):
        self._configure_compute_nodes()
        self._check_runaway_and_idle_compute_nodes()
        self._drain_and_stop_nodes()
        timer = threading.Timer(self.CHECK_CONFIGURING_NODES_INTERVAL, self.start_configure_cluster_timer)
        timer.daemon = True
        timer.start()

    def start(self):
        self.start_configure_cluster_timer()

    def _set_nodes_draining(self, node_names):
        # type: (list[str]) -> ()
        self._removed_nodes.update(self._upper_strings(node_names))
        self._set_node_state(node_names, HpcState.Draining, "Draining")

    def _set_nodes_closing(self, node_names):
        # type: (list[str]) -> ()
        self._removed_nodes.update(self._upper_strings(node_names))
        self._set_node_state(node_names, HpcState.Closing, "Closing")

    def _set_nodes_closed(self, node_names):
        # type: (list[str]) -> ()
        self._removed_nodes.update(self._upper_strings(node_names))
        closed_nodes = self._set_node_state(node_names, HpcState.Closed, "Closed")
        if self._node_closed_callbacks:
            for callback in self._node_closed_callbacks:
                callback(closed_nodes)

    def _set_nodes_running(self, node_names):
        # type: (list[str]) -> ()
        self._set_node_state(node_names, HpcState.Running, "Running")

    def _set_node_state(self, node_names, node_state, state_name):
        # type: (list[str], HpcState, str) -> list[str]
        setted_nodes = []
        for node_name in node_names:
            u_hostname = node_name.upper()
            if u_hostname in self._heart_beat_table:
                if self._heart_beat_table[u_hostname].state != node_state:
                    old_state = self._heart_beat_table[u_hostname].state
                    self._heart_beat_table[u_hostname] = self._heart_beat_table[u_hostname]._replace(state=node_state)
                    setted_nodes.append(u_hostname)
                    self.logger.info("Host {} set to {} from {}.".format(
                        u_hostname, state_name, HpcState.Names[old_state]))
            else:
                self.logger.error("Host {} is not recognized. State {} Ignored.".format(u_hostname, state_name))
        return setted_nodes

    def _drain_and_stop_nodes(self):
        node_names_to_drain = []
        node_names_to_close = []
        for host in dict(self._heart_beat_table).itervalues():
            if host.state == HpcState.Draining:
                node_names_to_drain.append(host.hostname)
            elif host.state == HpcState.Closing:
                node_names_to_close.append(host.hostname)

        if node_names_to_drain:
            drained_node_names = self._drain_nodes_state_machine(node_names_to_drain)
            if drained_node_names:
                self.logger.info("Drained nodes:{}".format(drained_node_names))
                self._set_nodes_closing(drained_node_names)
                node_names_to_close += drained_node_names  # short-cut the drained nodes to close

        if node_names_to_close:
            closed_node_names, re_drain_node_names = self._close_node_state_machine(node_names_to_close)
            if closed_node_names:
                self.logger.info("Closed nodes:{}".format(closed_node_names))
                self._set_nodes_closed(closed_node_names)
            if re_drain_node_names:
                self.logger.info("Closed nodes failed:{}".format(closed_node_names))
                self._set_nodes_draining(closed_node_names)

    def _drain_nodes_state_machine(self, node_names):
        # type: (list[str]) -> list[str]
        # TODO: check missing nodes in this state machines
        self.logger.info("Draining nodes: {}".format(node_names))
        take_offline_node_list = []
        invalid_state_node_dict = {}
        drained_node_names = []
        node_status_list = self._hpc_client.get_node_status_exact(node_names)
        for node_status in node_status_list:
            node_name = node_status[HpcRestClient.NODE_STATUS_NODE_NAME_KEY]
            node_state = node_status[HpcRestClient.NODE_STATUS_NODE_STATE_KEY]
            if self._check_node_state_online(node_status):
                take_offline_node_list.append(node_name)
            elif self._check_node_state_offline(node_status):
                drained_node_names.append(node_name)
            else:
                invalid_state_node_dict[node_name] = node_state
        try:
            if invalid_state_node_dict:
                self.logger.info("Node(s) in invalid state when draining: {}".format(str(invalid_state_node_dict)))
            if take_offline_node_list:
                self.logger.info("Taking node(s) offline: {}".format(str(take_offline_node_list)))
                self._hpc_client.take_nodes_offline(take_offline_node_list)
        except:
            # Swallow all exceptions here. As we don't want any exception to prevent drained nodes to be closed
            self.logger.exception('Exception happened when draining compute node.')

        return drained_node_names

    def _close_node_state_machine(self, node_names):
        # type: (list[str]) -> (list[str], list[str])
        self.logger.info("Closing nodes: {}".format(node_names))
        to_remove_node_names = []
        re_drain_node_names = []
        closed_node = []
        node_status_list = self._hpc_client.get_node_status_exact(node_names)
        for node_status in node_status_list:
            node_name = node_status[HpcRestClient.NODE_STATUS_NODE_NAME_KEY]
            if self._check_node_health_unapproved(node_status):
                # already removed node
                closed_node.append(node_name)
            else:
                if not self._check_node_state_offline(node_status):
                    # node is not properly drained
                    re_drain_node_names.append(node_name)
                else:
                    to_remove_node_names.append(node_name)

        # more already removed nodes
        closed_node += [name for name in node_names if name not in (re_drain_node_names + to_remove_node_names)]

        try:
            if to_remove_node_names:
                self.logger.info("Remove node(s): {}".format(to_remove_node_names))
                self._hpc_client.remove_nodes(to_remove_node_names)
        except:
            # Swallow all exceptions here. As we don't want any exception to prevent removed nodes go to closed
            self.logger.exception('Exception happened when configuring compute node.')

        return closed_node, re_drain_node_names

    def _upper_strings(self, strs):
        # type: (Iterable[str]) -> Iterable[str]
        return (x.upper() for x in strs)

    def _check_node_health_unapproved(self, node_status):
        return node_status[
                   HpcRestClient.NODE_STATUS_NODE_HEALTH_KEY] == HpcRestClient.NODE_STATUS_NODE_HEALTH_UNAPPROVED_VALUE

    def _check_node_in_mesos_group(self, node_status):
        # type: (dict[str, any]) -> bool
        return self.MESOS_NODE_GROUP_NAME.upper() not in self._upper_strings(
            node_status[HpcRestClient.NODE_STATUS_NODE_GROUP_KEY])

    def _check_node_in_specified_group(self, node_status):
        # type: (dict[str, any]) -> bool
        return self._node_group.upper() not in self._upper_strings(
            node_status[
                HpcRestClient.NODE_STATUS_NODE_GROUP_KEY])

    def _check_node_state(self, node_status, target_state):
        # type: (dict[str, any], str) -> bool
        return node_status[HpcRestClient.NODE_STATUS_NODE_STATE_KEY] == target_state

    def _check_node_state_offline(self, node_status):
        # type: (dict[str, any]) -> bool
        return self._check_node_state(node_status, HpcRestClient.NODE_STATUS_NODE_STATE_OFFLINE_VALUE)

    def _check_node_state_online(self, node_status):
        # type: (dict[str, any]) -> bool
        return self._check_node_state(node_status, HpcRestClient.NODE_STATUS_NODE_STATE_ONLINE_VALUE)

SlaveInfo = namedtuple("SlaveInfo", "hostname fqdn agent_id task_id cpus last_heartbeat state")


class HpcState:
    Unknown, Provisioning, Configuring, Running, Draining, Closing, Closed = range(7)
    Names = ["Unknown", "Provisioning", "Configuring", "Running", "Draining", "Closing", "Closed"]
