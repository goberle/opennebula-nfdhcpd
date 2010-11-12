#!/usr/bin/env python
#

# nfdcpd: A promiscuous, NFQUEUE-based DHCP server for virtual machine hosting
# Copyright (c) 2010 GRNET SA
#
#    This program is free software; you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation; either version 2 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License along
#    with this program; if not, write to the Free Software Foundation, Inc.,
#    51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#

import os
import re
import glob
import logging
import logging.handlers
import subprocess

import daemon
import nfqueue
import pyinotify

import IPy
from select import select
from socket import AF_INET, AF_INET6

from scapy.layers.l2 import Ether
from scapy.layers.inet import IP, UDP
from scapy.layers.inet6 import *
from scapy.layers.dhcp import BOOTP, DHCP
from scapy.sendrecv import sendp

DEFAULT_PATH = "/var/run/ganeti-dhcpd"
DEFAULT_NFQUEUE_NUM = 42
DEFAULT_USER = "nobody"
DEFAULT_LEASE_TIME = 604800 # 1 week
DEFAULT_RENEWAL_TIME = 600  # 10 min

LOG_FILENAME = "/var/log/nfdhcpd/nfdhcpd.log"

SYSFS_NET = "/sys/class/net"
DHCP_DUMMY_SERVER_IP = "1.2.3.4"

LOG_FORMAT = "%(asctime)-15s %(levelname)-6s %(message)s"

DHCPDISCOVER = 1
DHCPOFFER = 2
DHCPREQUEST = 3
DHCPDECLINE = 4
DHCPACK = 5
DHCPNAK = 6
DHCPRELEASE = 7
DHCPINFORM = 8

DHCP_TYPES = {
    DHCPDISCOVER: "DHCPDISCOVER",
    DHCPOFFER: "DHCPOFFER",
    DHCPREQUEST: "DHCPREQUEST",
    DHCPDECLINE: "DHCPDECLINE",
    DHCPACK: "DHCPACK",
    DHCPNAK: "DHCPNAK",
    DHCPRELEASE: "DHCPRELEASE",
    DHCPINFORM: "DHCPINFORM",
}

DHCP_REQRESP = {
    DHCPDISCOVER: DHCPOFFER,
    DHCPREQUEST: DHCPACK,
    DHCPINFORM: DHCPACK,
    }

class ClientFileHandler(pyinotify.ProcessEvent):
    def __init__(self, server):
        pyinotify.ProcessEvent.__init__(self)
        self.server = server

    def process_IN_DELETE(self, event):
        self.server.remove_iface(event.name)

    def process_IN_CLOSE_WRITE(self, event):
        self.server.add_iface(os.path.join(event.path, event.name))


class Client(object):
    def __init__(self, mac=None, ips=None, link=None, hostname=None):
        self.mac = mac
        self.ips = ips
        self.hostname = hostname
        self.link = link
        self.iface = None

    @property
    def ip(self):
        return self.ips[0]

    def is_valid(self):
        return self.mac is not None and self.ips is not None\
               and self.hostname is not None


class Subnet(object):
    def __init__(self, net=None, gw=None, dev=None):
        if isinstance(net, str):
            self.net = IPy.IP(net)
        else:
            self.net = net
        self.gw = gw
        self.dev = dev

    @property
    def netmask(self):
        return str(self.net.netmask())

    @property
    def broadcast(self):
        return str(self.net.broadcast())

    @property
    def prefix(self):
        return self.net.net()

    @property
    def prefixlen(self):
        return self.net.prefixlen()

    @staticmethod
    def _make_eui64(net, mac):
        """ Compute an EUI-64 address from an EUI-48 (MAC) address

        """
        comp = mac.split(":")
        prefix = IPy.IP(net).net().strFullsize().split(":")[:4]
        eui64 = comp[:3] + ["ff", "fe"] + comp[3:]
        eui64[0] = "%02x" % (int(eui64[0], 16) ^ 0x02)
        for l in range(0, len(eui64), 2):
            prefix += ["".join(eui64[l:l+2])]
        return IPy.IP(":".join(prefix))

    def make_eui64(self, mac):
        return self._make_eui64(self.net, mac)

    def make_ll64(self, mac):
        return self._make_eui64("fe80::", mac)


class VMNetProxy(object):
    def __init__(self, data_path, dhcp_queue_num=None,
                 rs_queue_num=None, ns_queue_num=None):
        self.data_path = data_path
        self.clients = {}
        self.subnets = {}
        self.ifaces = {}
        self.v6nets = {}
        self.nfq = {}

        # Inotify setup
        self.wm = pyinotify.WatchManager()
        mask = pyinotify.EventsCodes.ALL_FLAGS["IN_DELETE"]
        mask |= pyinotify.EventsCodes.ALL_FLAGS["IN_CLOSE_WRITE"]
        handler = ClientFileHandler(self)
        self.notifier = pyinotify.Notifier(self.wm, handler)
        self.wm.add_watch(self.data_path, mask, rec=True)

        # NFQUEUE setup
        if dhcp_queue_num is not None:
            self._setup_nfqueue(dhcp_queue_num, AF_INET, self.dhcp_response)

        if rs_queue_num is not None:
            self._setup_nfqueue(rs_queue_num, AF_INET6, self.rs_response)

        if ns_queue_num is not None:
            self._setup_nfqueue(ns_queue_num, AF_INET6, self.ns_response)

    def _setup_nfqueue(self, queue_num, family, callback):
        logging.debug("Setting up NFQUEUE for queue %d, AF %s" %
                      (queue_num, family))
        q = nfqueue.queue()
        q.set_callback(callback)
        q.fast_open(queue_num, family)
        q.set_queue_maxlen(5000)
        # This is mandatory for the queue to operate
        q.set_mode(nfqueue.NFQNL_COPY_PACKET)
        self.nfq[q.get_fd()] = q

    def build_config(self):
        self.clients.clear()
        self.subnets.clear()

        for file in glob.glob(os.path.join(self.data_path, "*")):
            self.add_iface(file)

    def get_ifindex(self, iface):
        """ Get the interface index from sysfs

        """
        file = os.path.abspath(os.path.join(SYSFS_NET, iface, "ifindex"))
        if not file.startswith(SYSFS_NET):
            return None

        ifindex = None

        try:
            f = open(file, 'r')
            ifindex = int(f.readline().strip())
            f.close()
        except:
            pass

        return ifindex


    def get_iface_hw_addr(self, iface):
        """ Get the interface hardware address from sysfs

        """
        file = os.path.abspath(os.path.join(SYSFS_NET, iface, "address"))
        if not file.startswith(SYSFS_NET):
            return None

        addr = None
        try:
            f = open(file, 'r')
            addr = f.readline().strip()
            f.close()
        except:
            pass
        return addr

    def parse_routing_table(self, table="main", family=4):
        """ Parse the given routing table to get connected route, gateway and
        default device.

        """
        ipro = subprocess.Popen(["ip", "-%d" % family, "ro", "ls",
                                 "table", table], stdout=subprocess.PIPE)
        routes = ipro.stdout.readlines()

        def_gw = None
        def_dev = None
        def_net = None

        for route in routes:
            match = re.match(r'^default.*via ([^\s]+).*dev ([^\s]+)', route)
            if match:
                def_gw, def_dev = match.groups()
                break

        for route in routes:
            # Find the least-specific connected route
            try:
                def_net = re.match("^([^\\s]+) dev %s" %
                                   def_dev, route).groups()[0]
                def_net = IPy.IP(def_net)
            except:
                pass

        return Subnet(net=def_net, gw=def_gw, dev=def_dev)

    def parse_binding_file(self, path):
        """ Read a client configuration from a tap file

        """
        try:
            iffile = open(path, 'r')
        except:
            return (None, None, None, None)
        mac = None
        ips = None
        link = None
        hostname = None

        for line in iffile:
            if line.startswith("IP="):
                ip = line.strip().split("=")[1]
                ips = ip.split()
            elif line.startswith("MAC="):
                mac = line.strip().split("=")[1]
            elif line.startswith("LINK="):
                link = line.strip().split("=")[1]
            elif line.startswith("HOSTNAME="):
                hostname = line.strip().split("=")[1]

        return Client(mac=mac, ips=ips, link=link, hostname=hostname)

    def add_iface(self, path):
        """ Add an interface to monitor

        """
        iface = os.path.basename(path)

        logging.debug("Updating configuration for %s" % iface)
        binding = self.parse_binding_file(path)
        ifindex = self.get_ifindex(iface)

        if ifindex is None:
            logging.warn("Stale configuration for %s found" % iface)
        else:
            if binding.is_valid():
                binding.iface = iface
                self.clients[binding.mac] = binding
                self.subnets[binding.link] = self.parse_routing_table(
                                                binding.link)
                logging.debug("Added client %s on %s" %
                              (binding.hostname, iface))
                self.ifaces[ifindex] = iface
                self.v6nets[iface] = self.parse_routing_table(binding.link, 6)

    def remove_iface(self, iface):
        """ Cleanup clients on a removed interface

        """
        if iface in self.v6nets:
            del self.v6nets.iface

        for mac in self.clients.keys():
            if self.clients[mac].iface == iface:
                del self.clients[mac]

        for ifindex in self.ifaces.keys():
            if self.ifaces[ifindex] == iface:
                del self.ifaces[ifindex]

        logging.debug("Removed interface %s" % iface)

    def dhcp_response(self, i, payload):
        """ Generate a reply to a BOOTP/DHCP request

        """
        # Decode the response - NFQUEUE relays IP packets
        pkt = IP(payload.get_data())

        # Get the actual interface from the ifindex
        iface = self.ifaces[payload.get_indev()]

        # Signal the kernel that it shouldn't further process the packet
        payload.set_verdict(nfqueue.NF_DROP)

        # Get the client MAC address
        resp = pkt.getlayer(BOOTP).copy()
        hlen = resp.hlen
        mac = resp.chaddr[:hlen].encode("hex")
        mac, _ = re.subn(r'([0-9a-fA-F]{2})', r'\1:', mac, hlen-1)

        # Server responses are always BOOTREPLYs
        resp.op = "BOOTREPLY"
        del resp.payload

        try:
            binding = self.clients[mac]
        except KeyError:
            logging.warn("Invalid client %s on %s" % (mac, iface))
            return

        if iface != binding.iface:
            logging.warn("Received spoofed DHCP request for %s from interface"
                         " %s instead of %s" %
                         (mac, iface, binding.iface))
            return

        resp = Ether(dst=mac, src=self.get_iface_hw_addr(iface))/\
               IP(src=DHCP_DUMMY_SERVER_IP, dst=binding.ip)/\
               UDP(sport=pkt.dport, dport=pkt.sport)/resp
        subnet = self.subnets[binding.link]

        if not DHCP in pkt:
            logging.warn("Invalid request from %s on %s, no DHCP"
                         " payload found" % (binding.mac, iface))
            return

        dhcp_options = []
        requested_addr = binding.ip
        for opt in pkt[DHCP].options:
            if type(opt) is tuple and opt[0] == "message-type":
                req_type = opt[1]
            if type(opt) is tuple and opt[0] == "requested_addr":
                requested_addr = opt[1]

        logging.info("%s from %s on %s" %
                    (DHCP_TYPES.get(req_type, "UNKNOWN"), binding.mac, iface))

        if req_type == DHCPREQUEST and requested_addr != binding.ip:
            resp_type = DHCPNAK
            logging.info("Sending DHCPNAK to %s on %s: requested %s"
                         " instead of %s" %
                         (binding.mac, iface, requested_addr, binding.ip))

        elif req_type in (DHCPDISCOVER, DHCPREQUEST):
            resp_type = DHCP_REQRESP[req_type]
            resp.yiaddr = self.clients[mac].ip
            dhcp_options += [
                 ("hostname", binding.hostname),
                 ("domain", binding.hostname.split('.', 1)[-1]),
                 ("router", subnet.gw),
                 ("name_server", "194.177.210.10"),
                 ("name_server", "194.177.210.211"),
                 ("broadcast_address", str(subnet.broadcast)),
                 ("subnet_mask", str(subnet.netmask)),
                 ("renewal_time", DEFAULT_RENEWAL_TIME),
                 ("lease_time", DEFAULT_LEASE_TIME),
            ]

        elif req_type == DHCPINFORM:
            resp_type = DHCP_REQRESP[req_type]
            dhcp_options += [
                 ("hostname", binding.hostname),
                 ("domain", binding.hostname.split('.', 1)[-1]),
                 ("name_server", "194.177.210.10"),
                 ("name_server", "194.177.210.211"),
            ]

        elif req_type == DHCPRELEASE:
            # Log and ignore
            logging.info("DHCPRELEASE from %s on %s" %
                         (binding.mac, iface))
            return

        # Finally, always add the server identifier and end options
        dhcp_options += [
            ("message-type", resp_type),
            ("server_id", DHCP_DUMMY_SERVER_IP),
            "end"
        ]
        resp /= DHCP(options=dhcp_options)

        logging.info("%s to %s (%s) on %s" %
                      (DHCP_TYPES[resp_type], mac, binding.ip, iface))
        sendp(resp, iface=iface, verbose=False)

    def rs_response(self, i, payload):
        """ Generate a reply to a BOOTP/DHCP request

        """
        # Get the actual interface from the ifindex
        iface = self.ifaces[payload.get_indev()]
        ifmac = self.get_iface_hw_addr(iface)
        subnet = self.v6nets[iface]
        ifll = subnet.make_ll64(ifmac)

        # Signal the kernel that it shouldn't further process the packet
        payload.set_verdict(nfqueue.NF_DROP)

        resp = Ether(src=self.get_iface_hw_addr(iface))/\
               IPv6(src=str(ifll))/ICMPv6ND_RA(routerlifetime=14400)/\
               ICMPv6NDOptPrefixInfo(prefix=str(subnet.prefix),
                                     prefixlen=subnet.prefixlen)

        logging.info("RA on %s for %s" % (iface, subnet.net))
        sendp(resp, iface=iface, verbose=False)

    def ns_response(self, i, payload):
        """ Generate a reply to an ICMPv6 neighbor solicitation

        """
        # Get the actual interface from the ifindex
        iface = self.ifaces[payload.get_indev()]
        ifmac = self.get_iface_hw_addr(iface)
        subnet = self.v6nets[iface]
        ifll = subnet.make_ll64(ifmac)

        ns = IPv6(payload.get_data())

        if not (subnet.net.overlaps(ns.tgt) or str(ns.tgt) == str(ifll)):
            logging.debug("Received NS for a non-routable IP (%s)" % ns.tgt)
            payload.set_verdict(nfqueue.NF_ACCEPT)
            return 1

        payload.set_verdict(nfqueue.NF_DROP)

        resp = Ether(src=ifmac, dst=ns.lladdr)/\
               IPv6(src=str(ifll), dst=ns.src)/\
               ICMPv6ND_NA(R=1, O=0, S=1, tgt=ns.tgt)/\
               ICMPv6NDOptDstLLAddr(lladdr=ifmac)

        logging.info("NA on %s for %s" % (iface, ns.tgt))
        sendp(resp, iface=iface, verbose=False)
        return 1

    def serve(self):
        """ Loop forever, serving DHCP requests

        """
        self.build_config()

        iwfd = self.notifier._fd

        while True:
            rlist, _, xlist = select(self.nfq.keys() + [iwfd], [], [], 1.0)
            # First check if there are any inotify (= configuration change)
            # events
            if iwfd in rlist:
                self.notifier.read_events()
                self.notifier.process_events()
                rlist.remove(iwfd)

            for fd in rlist:
                self.nfq[fd].process_pending()


if __name__ == "__main__":
    import optparse
    from capng import *
    from pwd import getpwnam, getpwuid

    parser = optparse.OptionParser()
    parser.add_option("-p", "--path", dest="data_path",
                      help="The location of the data files", metavar="DIR",
                      default=DEFAULT_PATH)
    parser.add_option("-c", "--dhcp-queue", dest="dhcp_queue",
                      help="The nfqueue to receive DHCP requests from"
                           " (default: %d" % DEFAULT_NFQUEUE_NUM, type="int",
                      metavar="NUM", default=DEFAULT_NFQUEUE_NUM)
    parser.add_option("-r", "--rs-queue", dest="rs_queue",
                      help="The nfqueue to receive IPv6 router"
                           " solicitations from (default: %d)" %
                           DEFAULT_NFQUEUE_NUM, type="int",
                      metavar="NUM", default=DEFAULT_NFQUEUE_NUM)
    parser.add_option("-n", "--ns-queue", dest="ns_queue",
                      help="The nfqueue to receive IPv6 neighbor"
                           " solicitations from (default: %d)" %
                           DEFAULT_NFQUEUE_NUM, type="int",
                      metavar="NUM", default=44)
    parser.add_option("-u", "--user", dest="user",
                      help="An unprivileged user to run as",
                      metavar="UID", default=DEFAULT_USER)
    parser.add_option("-d", "--debug", action="store_true", dest="debug",
                      help="Turn on debugging messages")
    parser.add_option("-f", "--foreground", action="store_false", dest="daemonize",
                      default=True, help="Do not daemonize, stay in the foreground")


    opts, args = parser.parse_args()

    if opts.daemonize:
        d = daemon.DaemonContext()
        d.open()

    pidfile = open("/var/run/nfdhcpd.pid", "w")
    pidfile.write("%s" % os.getpid())
    pidfile.close()

    logger = logging.getLogger()
    if opts.debug:
        logger.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.INFO)

    if opts.daemonize:
        handler = logging.handlers.RotatingFileHandler(LOG_FILENAME,
                                                       maxBytes=2097152)
    else:
        handler = logging.StreamHandler()

    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logger.addHandler(handler)

    logging.info("Starting up")
    proxy = VMNetProxy(opts.data_path, opts.dhcp_queue,
                       opts.rs_queue, opts.ns_queue)

    # Drop all capabilities except CAP_NET_RAW and change uid
    try:
        uid = getpwuid(int(opts.user))
    except ValueError:
        uid = getpwnam(opts.user)

    logging.info("Setting capabilities and changing uid")
    logging.debug("User: %s, uid: %d, gid: %d" %
                  (opts.user, uid.pw_uid, uid.pw_gid))
    capng_clear(CAPNG_SELECT_BOTH)
    capng_update(CAPNG_ADD, CAPNG_EFFECTIVE|CAPNG_PERMITTED, CAP_NET_RAW)
    capng_change_id(uid.pw_uid, uid.pw_gid,
                    CAPNG_DROP_SUPP_GRP | CAPNG_CLEAR_BOUNDING)
    logging.info("Ready to serve requests")
    proxy.serve()


# vim: set ts=4 sts=4 sw=4 et :