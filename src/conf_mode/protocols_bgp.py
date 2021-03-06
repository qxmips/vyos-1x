#!/usr/bin/env python3
#
# Copyright (C) 2020-2021 VyOS maintainers and contributors
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 or later as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os

from sys import exit

from vyos.config import Config
from vyos.configdict import dict_merge
from vyos.template import render_to_string
from vyos.util import call
from vyos.util import dict_search
from vyos import ConfigError
from vyos import frr
from vyos import airbag
airbag.enable()

frr_daemon = 'bgpd'

def get_config(config=None):
    if config:
        conf = config
    else:
        conf = Config()
    base = ['protocols', 'bgp']
    bgp = conf.get_config_dict(base, key_mangling=('-', '_'), get_first_key=True)

    # Bail out early if configuration tree does not exist
    if not conf.exists(base):
        return bgp

    # We also need some additional information from the config,
    # prefix-lists and route-maps for instance.
    base = ['policy']
    tmp = conf.get_config_dict(base, key_mangling=('-', '_'))
    # As we only support one ASN (later checked in begin of verify()) we add the
    # new information only to the first AS number
    asn = next(iter(bgp))
    # Merge policy dict into bgp dict
    bgp[asn] = dict_merge(tmp, bgp[asn])

    return bgp

def verify_remote_as(peer_config, asn_config):
    if 'remote_as' in peer_config:
        return peer_config['remote_as']

    if 'peer_group' in peer_config:
        peer_group_name = peer_config['peer_group']
        tmp = dict_search(f'peer_group.{peer_group_name}.remote_as', asn_config)
        if tmp: return tmp

    if 'interface' in peer_config:
        if 'remote_as' in peer_config['interface']:
            return peer_config['interface']['remote_as']

        if 'peer_group' in peer_config['interface']:
            peer_group_name = peer_config['interface']['peer_group']
            tmp = dict_search(f'peer_group.{peer_group_name}.remote_as', asn_config)
            if tmp: return tmp

    return None

def verify(bgp):
    if not bgp:
        return None

    # Check if declared more than one ASN
    if len(bgp) > 1:
        raise ConfigError('Only one BGP AS number can be defined!')

    for asn, asn_config in bgp.items():
        # Common verification for both peer-group and neighbor statements
        for neighbor in ['neighbor', 'peer_group']:
            # bail out early if there is no neighbor or peer-group statement
            # this also saves one indention level
            if neighbor not in asn_config:
                continue

            for peer, peer_config in asn_config[neighbor].items():
                # Only regular "neighbor" statement can have a peer-group set
                # Check if the configure peer-group exists
                if 'peer_group' in peer_config:
                    peer_group = peer_config['peer_group']
                    if peer_group not in asn_config['peer_group']:
                        raise ConfigError(f'Specified peer-group "{peer_group}" for '\
                                          f'neighbor "{neighbor}" does not exist!')


                # Some checks can/must only be done on a neighbor and not a peer-group
                if neighbor == 'neighbor':
                    # remote-as must be either set explicitly for the neighbor
                    # or for the entire peer-group
                    if not verify_remote_as(peer_config, asn_config):
                        raise ConfigError(f'Neighbor "{peer}" remote-as must be set!')

                for afi in ['ipv4_unicast', 'ipv6_unicast', 'l2vpn_evpn']:
                    # Bail out early if address family is not configured
                    if 'address_family' not in peer_config or afi not in peer_config['address_family']:
                        continue

                    afi_config = peer_config['address_family'][afi]
                    # Validate if configured Prefix list exists
                    if 'prefix_list' in afi_config:
                        for tmp in ['import', 'export']:
                            if tmp not in afi_config['prefix_list']:
                                # bail out early
                                continue
                            # get_config_dict() mangles all '-' characters to '_' this is legitimate, thus all our
                            # compares will run on '_' as also '_' is a valid name for a prefix-list
                            prefix_list = afi_config['prefix_list'][tmp].replace('-', '_')
                            if afi == 'ipv4_unicast':
                                if dict_search(f'policy.prefix_list.{prefix_list}', asn_config) == None:
                                    raise ConfigError(f'prefix-list "{prefix_list}" used for "{tmp}" does not exist!')
                            elif afi == 'ipv6_unicast':
                                if dict_search(f'policy.prefix_list6.{prefix_list}', asn_config) == None:
                                    raise ConfigError(f'prefix-list6 "{prefix_list}" used for "{tmp}" does not exist!')

                    if 'route_map' in afi_config:
                        for tmp in ['import', 'export']:
                            if tmp in afi_config['route_map']:
                                # get_config_dict() mangles all '-' characters to '_' this is legitim, thus all our
                                # compares will run on '_' as also '_' is a valid name for a route-map
                                route_map = afi_config['route_map'][tmp].replace('-', '_')
                                if dict_search(f'policy.route_map.{route_map}', asn_config) == None:
                                    raise ConfigError(f'route-map "{route_map}" used for "{tmp}" does not exist!')

                    if 'route_reflector_client' in afi_config:
                        if 'remote_as' in peer_config and asn != peer_config['remote_as']:
                            raise ConfigError('route-reflector-client only supported for iBGP peers')
                        else:
                            if 'peer_group' in peer_config:
                                peer_group_as = dict_search(f'peer_group.{peer_group}.remote_as', asn_config)
                                if peer_group_as != None and peer_group_as != asn:
                                    raise ConfigError('route-reflector-client only supported for iBGP peers')

        # Throw an error if a peer group is not configured for allow range
        for prefix in dict_search('listen.range', asn_config) or []:
            # we can not use dict_search() here as prefix contains dots ...
            if 'peer_group' not in asn_config['listen']['range'][prefix]:
                raise ConfigError(f'Listen range for prefix "{prefix}" has no peer group configured.')
            else:
                peer_group = asn_config['listen']['range'][prefix]['peer_group']
                # the peer group must also exist
                if not dict_search(f'peer_group.{peer_group}', asn_config):
                    raise ConfigError(f'Peer-group "{peer_group}" for listen range "{prefix}" does not exist!')

    return None

def generate(bgp):
    if not bgp:
        bgp['new_frr_config'] = ''
        return None

    # only one BGP AS is supported, so we can directly send the first key
    # of the config dict
    asn = list(bgp.keys())[0]
    bgp[asn]['asn'] = asn

    bgp['new_frr_config'] = render_to_string('frr/bgp.frr.tmpl', bgp[asn])
    return None

def apply(bgp):
    # Save original configuration prior to starting any commit actions
    frr_cfg = frr.FRRConfig()
    frr_cfg.load_configuration(frr_daemon)
    frr_cfg.modify_section(f'^router bgp \d+$', '')
    frr_cfg.add_before(r'(ip prefix-list .*|route-map .*|line vty)', bgp['new_frr_config'])
    frr_cfg.commit_configuration(frr_daemon)

    # If FRR config is blank, rerun the blank commit x times due to frr-reload
    # behavior/bug not properly clearing out on one commit.
    if bgp['new_frr_config'] == '':
        for a in range(5):
            frr_cfg.commit_configuration(frr_daemon)

    return None

if __name__ == '__main__':
    try:
        c = get_config()
        verify(c)
        generate(c)
        apply(c)
    except ConfigError as e:
        print(e)
        exit(1)
