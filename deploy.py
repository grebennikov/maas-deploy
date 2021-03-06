#!/usr/bin/env python3

# maas-deploy
# Copyright (C) 2018  Domingues Luis
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import maas.client
import os
import sys
import argparse
import yaml

BLOCK_SIZE = 4*1024**2

def cleanup_machine(machine):
    for interface in machine.interfaces:
        if interface.type == maas.client.enum.InterfaceType.BOND:
            interface.delete()
        elif interface.type == maas.client.enum.InterfaceType.PHYSICAL:
            interface.disconnect()

    for vg in machine.volume_groups:
        vg.delete()

    for disk in machine.block_devices:
        if disk.type == maas.client.enum.BlockDeviceType.VIRTUAL:
            disk.delete()

    for disk in machine.block_devices:
        if disk.type == maas.client.enum.BlockDeviceType.PHYSICAL:
            for partition in disk.partitions:
                partition.delete()

    machine.refresh()

def define_os_disks_raid1(machine, os_raid=None):
    os_disks = []

    # default disk discovery
    if os_raid is None:
        by_size = {}
        for disk in machine.block_devices:
            if disk.size in by_size.keys():
                by_size[disk.size].append(disk)
            else:
                by_size[disk.size] = [disk]

        found_os_disks = False
        for key in by_size:
            if len(by_size[key]) == 2:
                if not found_os_disks:
                    found_os_disks = True
                    os_disks = by_size[key]
                elif found_os_disks:
                    print("Ambiguous pair of disks")
                    sys.exit(-1)

        if not found_os_disks:
            print("Os disks can not be automatically discover")
            sys.exit(-1)

        return os_disks

    # disks defined
    if 'disks' in os_raid.keys() and len(os_raid['disks']) == 2:
        for disk in machine.block_devices:
            if disk.name in os_raid['disks']:
                os_disks.append(disk)
        if len(os_disks) != 2:
            print("Disks not properly defined")
            sys.exit(-1)

        return os_disks

def configure_os_disks_raid6(machine, os_raid6):
    os_disks = []
    if 'disks' in os_raid6.keys():
        for disk in machine.block_devices:
            if disk.name in os_raid6['disks']:
                os_disks.append(disk)
    else:
        print("Raid6 need to explicitly set the disks")
        sys.exit(-1)
    return os_disks

def configure_system_disks(machine, os_raid1=None, os_raid6=None, os_partitions=None):
    if os_raid6 is not None:
        raid_type = 6
        disks = configure_os_disks_raid6(machine, os_raid6)
        if "use_lvm" in os_raid6:
            use_lvm = os_raid6["use_lvm"]["enable"]
            lvm_name = os_raid6["use_lvm"]["name"]
        else:
            use_lvm = False
    else:
        raid_type = 1
        disks = define_os_disks_raid1(machine, os_raid1)
        if "use_lvm" in os_raid1:
            use_lvm = os_raid1["use_lvm"]["enable"]
            lvm_name = os_raid1["use_lvm"]["name"]
        else:
            use_lvm = False
    disks[0].set_as_boot_disk()
    partitions = []
    for disk in disks:
        # Align partition on 4 MiB blocks
        blocks = disk.available_size // BLOCK_SIZE
        size = blocks * BLOCK_SIZE - 1
        try:
            partitions.append(disk.partitions.create(size=size))
        except:
            partitions.append(disk.partitions.create(size=size-512000000))

    if raid_type == 1:
        raid = machine.raids.create(
            name="md0",
            level=maas.client.enum.RaidLevel.RAID_1,
            devices=partitions,
            spare_devices=[],
        )
    elif raid_type == 6:
        raid = machine.raids.create(
            name="md0",
            level=maas.client.enum.RaidLevel.RAID_6,
            devices=partitions,
            spare_devices=[],
        )

    if use_lvm:
        vg = machine.volume_groups.create(name=lvm_name, devices=[raid.virtual_device])
        for os_part, infos in os_partitions.items():
            lv = vg.logical_volumes.create(size=infos["size"], name="vg-sys"+os_part.replace('/', '-'))
            lv.format(infos["filesystem"])
            lv.mount(os_part)

    else:
        if os_partitions is None:
            raid.virtual_device.format("ext4")
            raid.virtual_device.mount("/")
        else:
            for os_part, infos in os_partitions.items():
                part = raid.virtual_device.partitions.create(infos["size"])
                part.format(infos["filesystem"])
                part.mount(os_part)

    machine.refresh()

def get_subnet(client, subnet_name):
    for subnet in client.subnets.list():
        if subnet.name == subnet_name:
            return subnet

def get_fabric(client, fabric_name):
    for fabric in client.fabrics.list():
        if fabric.name == fabric_name:
            return fabric

def get_subnet(client, subnet_name):
    for subnet in client.subnets.list():
        if subnet.name == subnet_name:
            return subnet

def configure_vlans(machine, client, vname, vdata, bond, VLANS, default_gateway, mtu=1050):
    if 'subnet' in vdata:
        vif = machine.interfaces.create(
            name="bond0.%s" % vdata['vlan'],
            interface_type=maas.client.enum.InterfaceType.VLAN,
            parent=bond,
            vlan=VLANS[str(vdata['vlan'])]
        )

        iface = machine.interfaces.create(
            name="br-%s" % vname,
            interface_type=maas.client.enum.InterfaceType.BRIDGE,
            parent=vif,
            mtu=mtu
        )

        if 'ip' in vdata:
            iface.links.create(
                mode=maas.client.enum.LinkMode.STATIC,
                subnet=get_subnet(client, vdata['subnet']),
                ip_address=vdata['ip'],
                default_gateway=default_gateway
            )
    else:
        iface = machine.interfaces.create(
            name="br-%s" % vname,
            interface_type=maas.client.enum.InterfaceType.BRIDGE,
            parent=bond,
            mtu=mtu
        )

def configure_network(machine, client, net_bonding=None, admin_net='None'):
    if admin_net is None:
        admin_net = "None"

    if len(machine.boot_interface.links) > 0:
        machine.boot_interface.links[0].delete()

    if admin_net != 'None':
        def_sub = get_subnet(client, admin_net)
        machine.boot_interface.links.create(mode=maas.client.enum.LinkMode.DHCP, subnet=def_sub)

    if net_bonding is not None:
        parents = []
        for interface in machine.interfaces:
            if interface.name in net_bonding['slaves']:
                parents.append(interface)

        for parent in parents:
            parent.disconnect()

        # Workarroung for Match bug in systemd, by overriding the mac
        # https://bugs.launchpad.net/netplan/+bug/1804861
        mac_address = '52:54:'+parents[0].mac_address[6:]

        bond = machine.interfaces.create(
            name=net_bonding['name'],
            mac_address=mac_address,
            interface_type=maas.client.enum.InterfaceType.BOND,
            parents=parents,
            bond_mode="802.3ad",
            bond_lacp_rate="fast",
            bond_xmit_hash_policy="layer3+4"
        )

        if 'vlans' in net_bonding:
            fabric = get_fabric(client, net_bonding['fabric'])
            VLANS = dict((vlan.name, vlan) for vlan in fabric.vlans)
            bond.vlan = fabric.vlans.get_default()
            bond.save()

            VLANS = dict((vlan.name, vlan) for vlan in fabric.vlans)
            for vname, vdata in net_bonding['vlans'].items():
                if 'default_dns' in vdata and vdata['default_dns']:
                    mtu = 1050 if 'mtu' not in vdata else vdata['mtu']
                    default_gateway = False if 'default_gateway' not in vdata else vdata['default_gateway']
                    configure_vlans(machine, client, vname, vdata, bond, VLANS, default_gateway, mtu)
                    break
            for vname, vdata in net_bonding['vlans'].items():
                if 'default_dns' not in vdata:
                    mtu = 1050 if 'mtu' not in vdata else vdata['mtu']
                    default_gateway = False if 'default_gateway' not in vdata else vdata['default_gateway']
                    configure_vlans(machine, client, vname, vdata, bond, VLANS, default_gateway, mtu)

    machine.refresh()


def configure_jbod_disks(machine, jbod_conf):
    for disk_conf in jbod_conf:
        for disk in machine.block_devices:
            if disk.name == disk_conf['device']:
                break
        part = disk.partitions.create(disk.available_size - 512000000)
        part.format(disk_conf['fs'])
        part.mount(disk_conf['mountpoint'])
        machine.refresh()


def configure_raid_array(machine, raid_array):
    partitions = []
    for disk in machine.block_devices:
        if disk.name in raid_array['disks']:
            part = disk.partitions.create(disk.available_size - 512000000)
            partitions.append(part)

    raid = machine.raids.create(
        name="md1",
        level=maas.client.enum.RaidLevel.RAID_6,
        devices=partitions,
        spare_devices=[]
    )

    raid.virtual_device.format(raid_array['fs'])
    raid.virtual_device.mount(raid_array['mountpoint'])

    machine.refresh()

def set_unused_disks(machine, user_data, unused_disks):
    if 'jbod_disks' in unused_disks:
        configure_jbod_disks(machine, unused_disks['jbod_disks'])
    if 'raid_array' in unused_disks:
        configure_raid_array(machine, unused_disks['raid_array'])
    if 'disk_array' in unused_disks:
        unused = ["/dev/" + device.name for device in machine.block_devices
                  if device.used_for == "Unused"]
        bootcmd = list(unused_disks['disk_array'])
        bootcmd.extend(unused)
        user_data.update({"bootcmd": [bootcmd]})
        if 'step2' in unused_disks:
            step2 = unused_disks['step2']
            user_data['bootcmd'].append(step2)

def build_user_data(machine, host_config):
    user_data = {}

    if 'user_data' in host_config:
        user_data = host_config['user_data']

    if 'unused_disks' in host_config:
        set_unused_disks(machine, user_data, host_config['unused_disks'])

    user_data = b"#cloud-config\n" + yaml.dump(user_data).encode("utf-8")
    return user_data

def get_item_configs(key, host_config):
    item = None
    if key in host_config:
        item = host_config[key]
    return item

def parse_config(host_config):
    if host_config is None:
        host_config = {}

    net_bonding = get_item_configs('net_bonding', host_config)
    os_raid1 = get_item_configs('os_raid1', host_config)
    os_raid6 = get_item_configs('os_raid6', host_config)
    os_partitions = get_item_configs('os_partitions', host_config)
    distro_name = get_item_configs('os', host_config)
    kernel_version = get_item_configs('kernel', host_config)
    admin_net = get_item_configs('admin_net', host_config)
    return net_bonding, os_raid1, os_raid6,os_partitions, distro_name, host_config, kernel_version, admin_net

def run_machine(hostname, yaml_config, client):
    for machine in client.machines.list():
        if machine.hostname == hostname:
            break
    else:
        print("No machine named %s found" % hostname)
        return

    if machine.status != maas.client.enum.NodeStatus.READY:
        print("machine %s is not READY" % machine.hostname)
        return
    print("Starting deployement of %s" % machine.hostname)
    config_items = parse_config(yaml_config)
    net_bonding = config_items[0]
    os_raid1 = config_items[1]
    os_raid6 = config_items[2]
    os_partitions = config_items[3]
    distro_name = config_items[4]
    host_config = config_items[5]
    kernel_version = config_items[6]
    admin_net = config_items[7]

    cleanup_machine(machine)
    configure_network(machine, client, net_bonding, admin_net)
    configure_system_disks(machine, os_raid1, os_raid6, os_partitions)

    machine.refresh()
    user_data = build_user_data(machine, host_config)
    machine.deploy(distro_series=distro_name, user_data=user_data, hwe_kernel=kernel_version)
    print("Machine %s is now in %s state." % (hostname, machine.status._name_ ))

def release_machine(hostname, client):
    for machine in client.machines.list():
        if machine.hostname == hostname:
            break
    print("Releasing %s" % hostname)
    machine.release()



def main():

    parser = argparse.ArgumentParser(description='Configure and deploy machines present in MaaS.')
    parser.add_argument("machines_config", help="List of the machines with their configuration")
    parser.add_argument("-r", "--release", help="Release all machines on the list", action="store_true")
    args = parser.parse_args()

    yaml_config = yaml.load(open(args.machines_config), Loader=yaml.FullLoader)

    client = maas.client.connect(
        os.getenv("MAAS_API_URL"),
        apikey=os.getenv("MAAS_API_KEY")
    )

    if args.release:
        print("Are you sure you want release " + str(list(yaml_config['machines'].keys()))+"?")
        print("You are running this command on " + os.getenv("MAAS_API_URL"))
        print("Type 'I am sure I want this!' all in upper case to continue.")
        msg = sys.stdin.readline()
        if msg == 'I AM SURE I WANT THIS!\n':
            for hostname in yaml_config['machines']:
                release_machine(hostname, client)
        else:
            print("Confirmation failed.")
    else:
        for hostname in yaml_config['machines']:
            run_machine(hostname, yaml_config['machines'][hostname], client)
    print("Script ended.")
if __name__ == "__main__":
    main()
