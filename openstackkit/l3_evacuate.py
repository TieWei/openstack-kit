#! /usr/bin/env python
# @author wtie@cisco.com
import logging
import os
import time
import itertools
import ansible.runner
from logging.handlers import SysLogHandler
from neutronclient.common.exceptions import NeutronClientException
from neutronclient.v2_0 import client


LOG = logging.getLogger('neutron-l3-evacuate')


def setup_logging(enable_debug):
    level = logging.INFO
    if enable_debug:
        level = logging.DEBUG
    logging.basicConfig(level=level, date_fmt='%m-%d %H:%M')
    syslog = SysLogHandler(address='/dev/log')
    syslog.setLevel(level)
    syslog_formatter = logging.Formatter('%(name)s: %(levelname)s %(message)s')
    syslog.setFormatter(syslog_formatter)
    LOG.addHandler(syslog)


def log_info(action, msg):
    LOG.info("[%-12s] - %s" % (action.upper(), msg))


def log_warn(action, msg):
    LOG.warning("[%-12s] - %s" % (action.upper(), msg))


def log_error(action, msg):
    LOG.error("[%-12s] - %s" % (action.upper(), msg))


def log_debug(action, msg):
    LOG.debug("[%-12s] - %s" % (action.upper(), msg))

# RemoteRunners - How to connect to remote server for checking


class RemoteRunner(object):

    def run(self, host, cmd):
        log_debug("run cmd", "run remote cmd [%s]: %s" % (host, cmd))
        rc, stdout, stderr = self.remote_exec(host, cmd)
        if rc != 0:
            return (False, stderr)
        else:
            return (True, stdout)


class AnsibleRemoteRunner(RemoteRunner):

    def remote_exec(self, host, cmd):
        results = ansible.runner.Runner(
            run_hosts=[host],
            module_name='shell',
            module_args=" ".join(cmd),
            timeout=12
        ).run()
        if host in results['contacted']:
            return (results['contacted'][host]['rc'],
                    results['contacted'][host]['stdout'],
                    results['contacted'][host]['stderr'])
        else:
            return (1, None, results['dark'][host])

    def service_exec(self, host, service, action):
        results = ansible.runner.Runner(
            run_hosts=[host],
            module_name='service',
            module_args={'name': service, 'state': action},
            timeout=12
        ).run()
        if host in results['contacted']:
            return (results['contacted'][host]['state'],
                    results['contacted'][host]['changed'])
        else:
            return (1, None, results['dark'][host])


# Pickers - How to select the destination for one router
class Picker(object):

    def __init__(self, neutron, src_agent):
        self.client = neutron
        agents = neutron.list_agents(agent_type='L3 agent',
                                     admin_state_up=True,
                                     alive=True).get('agents')
        self._src_agent = src_agent
        self.dest = {}
        for agent in agents:
            if agent['alive'] and agent['admin_state_up'] and \
                    agent['id'] != self._src_agent['id']:
                self.dest[agent['id']] = {}
                self.dest[agent['id']]['agent'] = agent
                self.dest[agent['id']]['routers'] = []
        self._dest_cycle = itertools.cycle(self.dest.keys())
        self.src_router_count = None

    def has_next_for_agent(self, agent):
        return len(self.dest[agent['id']]['routers']) > 0

    def get_next_for_agent(self, agent):
        if self.has_next_for_agent(agent):
            return (self.dest[agent['id']]['agent'],
                    self.dest[agent['id']]['routers'].pop())
        else:
            return (None, None)

    def get_next(self):
        candidate = len(self.dest)
        while candidate > 0:
            agent_id = self._dest_cycle.next()
            candidate -= 1
            if len(self.dest[agent_id]['routers']) > 0:
                return (self.dest[agent_id]['agent'],
                        self.dest[agent_id]['routers'].pop())
            else:
                continue
        return (None, None)

    def has_next(self):
        for agent_router in self.dest.values():
            if len(agent_router['routers']) > 0:
                return True
            else:
                continue
        return False


class BalancePicker(Picker):

    def init(self):
        routers = self.client.list_routers_on_l3_agent(
            self._src_agent['id']).get('routers', [])
        self.src_router_count = len(routers)
        totals = {}
        for agent_id in self.dest.keys():
            totals[agent_id] = self.dest[agent_id][
                'agent']['configurations']['routers']
        for router in routers:
            agent_id = min(
                totals.keys(), key=lambda agent_id: totals[agent_id])
            self.dest[agent_id]['routers'].append(router)
            totals[agent_id] += 1
        return len(routers)


class CyclePicker(Picker):

    def init(self):
        routers = self.client.list_routers_on_l3_agent(
            self._src_agent['id']).get('routers', [])
        self.src_router_count = len(routers)
        for router in routers:
            agent_id = self._dest_cycle.next()
            self.dest[agent_id]['routers'].append(router)
        return len(routers)


# Evacuator - How to migate routers
class L3AgentEvacuator(object):

    def __init__(self, **kwargs):
        if "insecure" in kwargs and kwargs['insecure'] is True:
            self._insecure_client = True
        else:
            self._insecure_client = False
        self._setup_neutron_client()
        if 'agent' not in kwargs and 'target' not in kwargs:
            raise Exception("Missing target hostname or agent id")
        else:
            target_agent_id_1 = None
            target_agent_id_2 = None
            if 'agent' in kwargs and kwargs['agent']:
                agent = kwargs['agent']
                target_agent_id_1 = self._get_agent_id(agent)
            if 'target' in kwargs and kwargs['target']:
                target = kwargs['target']
                target_agent_id_2 = self._get_agent_id(target)

            if target_agent_id_1 and target_agent_id_2:
                if target_agent_id_1 == target_agent_id_2:
                    agent_id = target_agent_id_1
                else:
                    raise Exception("target hostname not match agent id")
            elif target_agent_id_1:
                agent_id = target_agent_id_1
            elif target_agent_id_2:
                agent_id = target_agent_id_2
            else:
                raise Exception("Invalid target hostname or agent id")
        self._src_agent = self._neutron.show_agent(agent_id).get('agent', {})

        if 'stopl3' in kwargs and kwargs['stopl3'] is True:
            self._stop_agent_after_evacuate = True
        else:
            self._stop_agent_after_evacuate = False
        if "wait_interval" in kwargs:
            self._wait_interval = kwargs['wait_interval']
        else:
            self._wait_interval = 1
        if "wait_timeout" in kwargs:
            self._wait_timeout = kwargs['wait_timeout']
        else:
            self._wait_timeout = 30
        if "least_wait_time" in kwargs:
            self._least_wait_time = kwargs['least_wait_time']
        else:
            self._least_wait_time = 3
        if 'remote_runner' in kwargs:
            self._setup_remote_runner(kwargs['remote_runner'])
        else:
            self._setup_remote_runner('ansible')
        if 'picker' in kwargs:
            self._setup_picker(kwargs['picker'])
        else:
            self._setup_picker('balance')
        if 'retry' in kwargs and kwargs['retry'] >= 0:
            self._retry = kwargs['retry']
        else:
            self._retry = 1

    def _get_agent_id(self, hostname_or_id):
        agents = self._neutron.list_agents(agent_type='L3 agent').get('agents')
        for one_agent in agents:
            if one_agent['id'] == hostname_or_id or \
               one_agent['host'] == hostname_or_id:
                return one_agent['id']
        return None

    def _setup_neutron_client(self):
        ca = os.environ.get('OS_CACERT', None)

        self._neutron = client.Client(auth_url=os.environ['OS_AUTH_URL'],
                                      username=os.environ['OS_USERNAME'],
                                      tenant_name=os.environ['OS_TENANT_NAME'],
                                      password=os.environ['OS_PASSWORD'],
                                      endpoint_type='internalURL',
                                      insecure=self._insecure_client,
                                      ca_cert=ca)

    def _setup_picker(self, picker):
        if picker == 'cycle':
            self.picker = CyclePicker(self._neutron, self._src_agent)
        elif picker == 'balance':
            self.picker = BalancePicker(self._neutron, self._src_agent)
        else:
            raise Exception("No picker found for %s" % picker)

    def _setup_remote_runner(self, remote_runner):
        if remote_runner == 'ansible':
            self.remote_runner = AnsibleRemoteRunner()
        else:
            raise Exception("No remote runner found for %s" % remote_runner)

    def run(self):
        # start time
        start_time = time.time()
        log_info("start", "------ L3 agent evacuate start ------")
        # setup picker
        count = self.picker.init()
        # init status
        log_info("start", " %d routers need to be migrated off host %s" % (
            count, self._src_agent['host']))
        # do migrate
        self.evacuate()
        if self._stop_agent_after_evacuate:
            log_info("checking start",
                     "checking before stop neutron l3 agent service")
            left_routers = self._list_router_on_l3_agent(self._src_agent)
            if len(left_routers) == 0:
                # no new created routers, stop service
                log_info("checking complete",
                         "No new router scheduled to the agent, stopping...")
                self._stop_agent(self._src_agent['host'])
                log_info("service stop",
                         "Service neutron-l3-agent stopped on %s"
                         % self._src_agent['host'])
            else:
                # run the whole agent evacuate again
                log_info("summary", "Found %d new scheduled router on agent, "
                         "retry evacuating" % len(left_routers))
                self.run()
        else:
            log_info("summary", "")
            left_routers = self._list_router_on_l3_agent(self._src_agent)
            if left_routers:
                log_warn("summary",
                         "[%d] routers are not evacuated" % len(left_routers))
                for router in left_routers:
                    log_warn("summary", "router id %s" % router['id'])
        # end time
        end_time = time.time()
        evacuated = self.picker.src_router_count
        summary = "evacuated %d routers off agent %s [%s] in %d seconds" % (
            evacuated, self._src_agent['id'], self._src_agent['host'],
            end_time - start_time)
        log_info("summary", summary)
        log_info("completed", "------ L3 agent evacuate end ------")
        return summary

    def _list_router_on_l3_agent(self, agent):
        return self._neutron.list_routers_on_l3_agent(
            agent['id']).get('routers', [])

    def _wait_until(self, func, *args, **kwargs):
        wait_time = 0
        wait_timeout = self._wait_timeout
        wait_interval = self._wait_interval
        least_wait_time = self._least_wait_time
        if 'wait_timeout' in kwargs:
            wait_timeout = kwargs.pop('wait_timeout')
        if 'wait_interval' in kwargs:
            wait_interval = kwargs.pop('wait_interval')
        if 'least_wait_time' in kwargs:
            least_wait_time = kwargs.pop('least_wait_time')

        while wait_time < least_wait_time or wait_time <= wait_timeout:
            if not func(*args, **kwargs):
                wait_time += wait_interval
                time.sleep(wait_interval)
            else:
                break
        if wait_time > wait_timeout:
            return False
        else:
            return True

    def _check_api_removed(self, agent, router):
        log_debug("api checking", "checking router %s removed from agent %s "
                  "via api " % (router['id'], agent['id']))
        agents = self._neutron.list_l3_agent_hosting_routers(
            router['id']).get('agents', [])
        if len(agents) == 0:
            log_debug("api checking", "router %s removed from agent %s "
                      "successfully via api " % (router['id'], agent['id']))
            return True
        else:
            log_warn("api checking", "Router %s removed from agent %s failed "
                     "via api " % (router['id'], agent['id']))
            return False

    def _check_api_added(self, agent, router):
        log_debug("api checking", "checking router %s added to agent %s via "
                  "api " % (router['id'], agent['id']))
        agents = self._neutron.list_l3_agent_hosting_routers(
            router['id']).get('agents', [])
        if agent['id'] in [one_agent['id'] for one_agent in agents]:
            log_debug("api checking", "router %s added to %s successfully via "
                      "api" % (router['id'], agent['id']))
            return True
        else:
            log_warn("api checking", "router %s added to agent %s failed via "
                     "api" % (router['id'], agent['id']))
            return False

    def _ensure_clean_router_on_host(self, agent, router):
        log_debug("ensure router clean", "ensure router %s cleaned from "
                  "agent %s on host %s" % (router['id'], agent['id'],
                                           agent['host']))
        host = agent['host']
        namespace = "qrouter-%s" % router['id']
        result = self._list_nics_in_netns_on_remote(host, namespace)
        if len(result) == 0:
            log_debug("ensure router clean", "router %s remove verified from "
                      "agent %s on host %s" % (router['id'], agent['id'],
                                               agent['host']))
            return True
        else:
            log_warn("port clean", "router %s clean failed from agent %s on "
                     "host %s - nics %s not deleted" % (router['id'],
                                                        agent['id'],
                                                        agent['host'],
                                                        result))
            self._clean_nics_on_host(host, result)
            self._clean_netns_on_host(host, namespace)
            log_info("port clean", "router %s cleaned from agent %s on host %s"
                     " - nics %s force cleaned" % (router['id'], agent['id'],
                                                   agent['host'], result))
            return True

    def _verify_router_snat_rule(self, agent, router):
        host = agent['host']
        namespace = "qrouter-%s" % router['id']
        log_debug("verify wait", "Trying to find snat rule in namespace %s on "
                  "host [%s] as the mark of neutron finished updating" %
                  (namespace, host))
        cmd = self._cmd_grep_snat_rule_in_netns(namespace,
                                                'neutron-l3-agent-snat')
        rc, output = self.remote_runner.run(host, cmd)
        if rc:
            log_info("verify wait", "Found snat rule in namespace %s on host "
                     "[%s], neutron finished the router add" % (namespace, host))
            return True
        else:
            log_debug("verify wait", "Failed to find snat rule in namespace %s "
                      "on host [%s], waiting for neutron" % (namespace, host))
            return False

    def _cmd_grep_snat_rule_in_netns(self, netns, rule_name):
        return ["ip", "netns", "exec", netns, 'iptables', "-t",
                "nat", "-L", "|", "grep", "^%s" % rule_name]

    def _verify_router_on_host(self, agent, router):
        log_debug("router verify", "Verifying router %s added to agent %s on "
                  "host %s" % (router['id'], agent['id'], agent['host']))
        verify_ports = self._verify_ports_on_host(agent, router)
        if not verify_ports:
            return False
        verify_ip_forward = self._verify_ipforward_on_host(agent, router)
        return verify_ip_forward

    def _cmd_show_ipforward_in_netns(self, netns):
        return ["ip", "netns", "exec", netns, 'cat',
                '/proc/sys/net/ipv4/ip_forward']

    def _verify_ipforward_on_host(self, agent, router):
        host = agent['host']
        namespace = "qrouter-%s" % router['id']
        log_debug("router verify", "Start to verify ip forward in namespace %s "
                  "on host [%s]" % (namespace, host))
        rc, output = self.remote_runner.run(
            host, self._cmd_show_ipforward_in_netns(namespace))
        if rc:
            return output == "1"
        else:
            log_warn("router verify", "Failed to verify ip forward in "
                     "namespace %s on host [%s]" % (namespace, host))
            return False

    def _verify_ports_on_host(self, agent, router):
        host = agent['host']
        namespace = "qrouter-%s" % router['id']
        ports = self._neutron.list_ports(
            device_id=router['id'], admin_state_up=True).get('ports', [])
        if len(ports) == 0:
            return True
        state = True
        result = self._list_nics_in_netns_on_remote(host, namespace)
        for one_port in ports:
            if one_port['device_owner'] == 'network:router_interface':
                nic = "qr-%s" % one_port['id'][0:11]
            elif one_port['device_owner'] == 'network:router_gateway':
                nic = "qg-%s" % one_port['id'][0:11]
            else:
                continue
            verfied = nic in result
            log_debug("port verify", "verify router %s added to agent %s on "
                      "host %s - %s : %s" % (router['id'], agent['id'],
                                             agent['host'], nic, verfied))
            state = state & verfied
        return state

    def _cmd_list_nic_in_netns(self, netns):
        return ["ip", "netns", "exec", netns, "ls", "-1", "/sys/class/net/"]

    def _list_nics_in_netns_on_remote(self, host, netns):
        rc, output = self.remote_runner.run(
            host, self._cmd_list_nic_in_netns(netns))
        if rc:
            nics = []
            for one_nic in output.split('\n'):
                if one_nic != 'lo':
                    nics.append(one_nic)
            return nics
        else:
            if 'Cannot open network namespace' in output:
                return []
            else:
                raise Exception("Failed list nics in namespace %s on host "
                                "[%s]" % (netns, host))

    def _cmd_delete_ovs_port(self, port_name, br_name="", timeout=10):
        return ["ovs-vsctl", "--timeout=%d" % timeout, "--", "--if-exists",
                "del-port", br_name, port_name]

    def _cmd_delete_netns(self, netns):
        return ["ip", "netns", "delete", netns]

    def _clean_nics_on_host(self, host, nics):
        for one_nic in nics:
            log_debug("port deleting", "start deleting port %s on host %s"
                      % (one_nic, host))
            if 'qg-' in one_nic:
                succeed, output = self.remote_runner.run(
                    host, self._cmd_delete_ovs_port(one_nic,
                                                    br_name='br-router'))
            elif 'qr-' in one_nic:
                succeed, output = self.remote_runner.run(
                    host, self._cmd_delete_ovs_port(one_nic,
                                                    br_name='br-int'))
            else:
                succeed, output = self.remote_runner.run(
                    host, self._cmd_delete_ovs_port(one_nic))
            if not succeed:
                log_warn(
                    "port deleting", "Failed to delete port %s on host %s - "
                    "%s" % (one_nic, host, output))

    def _clean_netns_on_host(self, host, namespace):
        succeed, output = self.remote_runner.run(
            host, self._cmd_delete_netns(namespace))
        if not succeed:
            log_warn("netns deleting",
                     "Failed to delete netns %s on host %s - %s" %
                     (namespace, host, output))

    def _stop_agent(self, host):
        service_exec = getattr(self.remote_runner, "service_exec", None)
        if callable(service_exec):
            state, _ = self.remote_runner.service_exec(host, 'neutron-l3-agent',
                                                       'stopped')
            if state == 'stopped':
                result = True
            else:
                result = False
        else:
            cmd = ["service", "neutron-l3-agent", "stop"]
            result, _ = self.remote_runner.run(host, cmd)
        if not result:
            log_error("service stop", "Failed to stop neutron-l3-agent")
        else:
            log_info("service stop", "Stopped neutron-l3-agent")


class SequenceEvacuator(L3AgentEvacuator):

    def _remove_router(self, agent, router, retry=0):
        log_debug("remove start", "remove router %s from %s" %
                  (router['id'], agent['id']))
        need_retry = False
        try:
            self._neutron.remove_router_from_l3_agent(
                agent['id'], router['id'])
            if not self._wait_until(self._check_api_removed, agent, router):
                need_retry = True
                log_warn("api remove failed",
                         "failed to remove router %s from agent %s"
                         % (router['id'], agent['id']))
        except NeutronClientException as e:
            need_retry = True
            log_warn("neutron exception", "remove router %s from "
                     "agent %s - %s" % (router['id'], agent['id'], e.message))
        finally:
            if not need_retry:
                return True
            if retry > 0:
                return self._remove_router(agent, router, retry=retry - 1)
            else:
                return False

    def _ensure_router_cleaned(self, agent, router):
        try:
            # clean left qg qr devices if they are not cleaned by neutron
            self._ensure_clean_router_on_host(agent, router)
            log_debug("remove ensured", "remove router %s from agent %s" %
                      (router['id'], agent['id']))
            return True
        except Exception as e:
            log_warn("ensure exception", "exception ensure clean router %s from agent"
                     " %s - %s" % (router['id'], agent['id'], e.message))
            return False

    def _add_router(self, agent, router, retry=0):
        log_debug("add start", "add router %s to agent %s" %
                  (router['id'], agent['id']))
        need_retry = False
        try:
            self._neutron.add_router_to_l3_agent(
                agent['id'], dict(router_id=router['id']))
            if not self._wait_until(self._check_api_added, agent, router):
                need_retry = True
                log_warn("api add failed", "failed add router %s to agent %s" %
                         (router['id'], agent['id']))
        except NeutronClientException as e:
            need_retry = True
            log_warn("neutron exception", "exception add router %s to agent %s - %s" %
                     (router['id'], agent['id'], e.message))
        finally:
            if not need_retry:
                return True
            if retry > 0:
                return self._add_router(agent, router, retry=retry - 1)
            else:
                return False

    def _ensure_router_added(self, agent, router):
        try:
            # wait until neutron did the change - by monitoring the iptables
            # timeout set as 15 seconds
            self._wait_until(self._verify_router_snat_rule, agent,
                             router, wait_timeout=15,
                             least_wait_time=1, wait_interval=1)

            # check the server if the routers setting is correct
            if not self._verify_router_on_host(agent, router):
                # wait again, since port with multi floating ip takes
                # time to add
                extra_timeout = self._extra_timeout_for_router(router)
                log_warn("verify add failed", "failed to verify router %s on "
                         "agent %s, wait for another %d seconds" %
                         (router['id'], agent['id'], extra_timeout))
                added = self._wait_until(self._verify_router_on_host,
                                         agent, router,
                                         wait_timeout=extra_timeout,
                                         least_wait_time=1, wait_interval=1)
                if not added:
                    log_error("verify add failed", "failed to add router %s on "
                              "agent %s, please verify manually" % (router['id'],
                                                                    agent['id']))
                    return False
                else:
                    log_debug("add complete", "add router %s to agent %s" %
                              (router['id'], agent['id']))
                    return True
            else:
                log_debug("add complete", "add router %s to agent %s" %
                          (router['id'], agent['id']))
                return True
        except Exception as e:
            log_error("verify error", "Error - check router %s on "
                      "agent %s - %s" % (router['id'], agent['id'], e.message))
            return True

    def _extra_timeout_for_router(self, router):
        ports = self._neutron.list_ports(
            device_id=router['id']).get('ports', [])
        floating_ips = self._neutron.list_floatingips(
            router_id=router['id']).get('floatingips', [])
        routes = router['routes']
        return len(ports) + len(floating_ips) + len(routes)

    def _retry_failed_router(self, router, src_agent, retry=0):
        if retry < 0:
            return
        agents = self._neutron.list_l3_agent_hosting_routers(
            router['id']).get('agents', [])
        if len(agents) > 0:
            # if it's hosted by any agent, just leave it there
            agent = agents[0]
            ensured = self._ensure_router_added(agent, router)
            if ensured:
                if agent['id'] != src_agent['id']:
                    self._ensure_router_cleaned(src_agent, router)
                return True
            else:
                log_error("add error", "Router %s migrate failed, need verify"
                          " manually" % router['id'])
        else:
            # add it back to src_agent
            self._add_router(src_agent, router, self._retry)
            # ignore it for now, let the next run migrate it
            self._retry_failed_router(router, src_agent, retry-1)

    def migrate_router(self, target_agent, router, src_agent=None):
        if not src_agent:
            src_agent = self._src_agent
        log_info("migrate start", "Start migrate router %s from %s to %s" % (
            router['id'], src_agent['id'], target_agent['id']))
        removed = self._remove_router(src_agent, router, self._retry)
        if removed:
            log_info("router removed", "Removed router %s from %s" % (
                router['id'], src_agent['id']))
            added = self._add_router(target_agent, router, self._retry)
            ports = self._neutron.list_ports(
                device_id=router['id'], admin_state_up=True).get('ports', [])
            if added and len(ports) > 0:
                # ensure the router is on the target host
                ensure_added = self._ensure_router_added(target_agent, router)
                # ensure the router is not on the source host
                if ensure_added:
                    self._ensure_router_cleaned(src_agent, router)
                else:
                    self._retry_failed_router(router, src_agent, self._retry)
            elif len(ports) == 0:
                # skip if no ports on the router
                pass
            else:
                self._retry_failed_router(router, src_agent, self._retry)
        else:
            # if remove failed, left it there for next loop
            log_warn("remove failed", "Failed remove router %s from %s by api" % (
                router['id'], src_agent['id']))

        log_info("migrate end", "End migrate router %s from %s to %s" %
                 (router['id'], self._src_agent['id'], target_agent['id']))

    def evacuate(self):
        while self.picker.has_next():
            agent, router = self.picker.get_next()
            self.migrate_router(agent, router)


if __name__ == '__main__':
    # ensure environment has necessary items to authenticate
    for key in ['OS_TENANT_NAME', 'OS_USERNAME', 'OS_PASSWORD',
                'OS_AUTH_URL']:
        if key not in os.environ.keys():
            LOG.exception("Your environment is missing '%s'")

    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("agent_id", help="l3 agent id to evacuate")
    parser.add_argument("--picker",
                        choices=['cycle', 'balance'],
                        help="method to distribute",
                        default='cycle')
    parser.add_argument("--runner",
                        choices=['ansible'],
                        help="method to run remote command",
                        default="ansible")
    parser.add_argument("--stopl3", action="store_true",
                        help="stop neutron-l3-agent after evacuate",
                        default=False)
    parser.add_argument('-d', '--debug', action='store_true',
                        default=False, help='Show debugging output')
    args = parser.parse_args()

    setup_logging(args.debug)
    SequenceEvacuator(args.agent_id, args.picker, args.runner,
                      stop_agent_after_evacuate=args.stopl3).run()
