from flask import Flask, request, jsonify
import subprocess
import string
import random
import redis
from os import getenv, path
from dotenv import load_dotenv
from shutil import chown
import iptc

from pathlib import Path

import ipaddress

env_path = Path('.') / '.env'
load_dotenv(dotenv_path=str(env_path))

REDIS_KEY_GROW_ID_COUNTER = 'grow-counter'
REDIS_KEY_GROWS_BY_IDENTIFIER = 'grows-by-identifier'
REDIS_KEY_GROW_CLIENT_COUNTER = 'grow-{}-client-counter'

ALLOWED_CLIENT_TYPES = ["administrator", "core", "sensor"]

PATH_TO_EASY_RSA = "/home/ubuntu/EasyRSA-3.0.4/"

PATH_TO_BASE_OPENVPN_CONFIG = "/home/ubuntu/clients/setup/base.conf"
PATH_TO_OPENVPN_CA = "/home/ubuntu/clients/setup/ca.crt"
PATH_TO_OPENVPN_TA_KEY = "/home/ubuntu/clients/setup/ta.key"

PATH_TO_OPENVPN_CONFIGS = "/home/ubuntu/clients/config/"

# FINISHED_KEY_LOCATION = '/home/ubuntu/client-configs/keys/'
# MAKE_CONFIG_EXECUTABLE = '/home/ubuntu/client-configs/make_config.sh'
# FINAL_OPENVPN_CONFIG_DIRECTORY = '/home/ubuntu/client-configs/files/'
OPENVPN_CLIENT_CONFIG_DIRECTORY = '/etc/openvpn/ccd/'
OPENVPN_USER = 'nobody'
OPENVPN_GROUP = 'nogroup'

GROW_STARTING_NETWORK = ipaddress.ip_address('13.0.0.0')
GROW_NETMASK = '255.255.240.0'  # Netmask of /20, aka 4096 subnets and 4094 hosts per subnet
NUMBER_OF_SUBNETS = 4096
NUMBER_OF_HOSTS = 4094

OWNED_BY_USER = 'ubuntu'

app = Flask(__name__)
redis_client = redis.StrictRedis(host='localhost', port=6379, db=0)


def id_generator(size=20, chars=string.ascii_uppercase + string.digits):
    return ''.join(random.choice(chars) for _ in range(size))


if not redis_client.get(REDIS_KEY_GROW_ID_COUNTER):
    redis_client.set(REDIS_KEY_GROW_ID_COUNTER, '0')


@app.route('/', methods=['POST', 'GET'])
def main():
    secret_key = request.args.get('secret_key')
    if secret_key != getenv("SECRET_KEY"):
        return b"Invalid secret_key GET parameter", 401
    if request.method == 'POST':
        if not request.json:
            return b"Must post JSON with Content-Type application/json", 400
        if "grow_id" not in request.json:
            return b"Missing `grow_id` in POSTed JSON", 400
        if "client_type" not in request.json or request.json["client_type"] not in ALLOWED_CLIENT_TYPES:
            return "Parameter `client_type` in POSTed JSON is invalid (must be one of {})".format(
                ", ".join(ALLOWED_CLIENT_TYPES)), 400
        grow_identifier = request.json["grow_id"]
        client_type = request.json["client_type"]
        grow_server_id = redis_client.hget(REDIS_KEY_GROWS_BY_IDENTIFIER, grow_identifier)
        if not grow_server_id:
            grow_server_id = redis_client.incr(REDIS_KEY_GROW_ID_COUNTER)
            redis_client.hset(REDIS_KEY_GROWS_BY_IDENTIFIER, grow_identifier, grow_server_id)
            # We set the default client counter to 2 to reserve the administrator IP address and core IP addresses,
            # respectively. From there we will increment up to NUMBER_OF_HOSTS before disabling
            # new clients to be added to this VPN server.
            redis_client.set(REDIS_KEY_GROW_CLIENT_COUNTER.format(grow_server_id), '2')
        else:
            grow_server_id = int(grow_server_id)
        client_name = '{}-{}'.format(grow_identifier, client_type)
        path_to_full_key = path.join(PATH_TO_EASY_RSA, 'pki/private', '{}.key'.format(client_name))
        path_to_full_cert = path.join(PATH_TO_EASY_RSA, 'pki/issued', '{}.crt'.format(client_name))
        # path_to_output_openvpn_config = path.join(FINAL_OPENVPN_CONFIG_DIRECTORY, '{}.ovpn'.format(client_name))
        path_to_client_config = path.join(OPENVPN_CLIENT_CONFIG_DIRECTORY, client_name)
        path_to_output_openvpn_config = path.join(PATH_TO_OPENVPN_CONFIGS, '{}.ovpn'.format(client_name))
        if not path.exists(path_to_output_openvpn_config):
            if not path.exists(path_to_full_key):
                try:
                    key_process = subprocess.Popen(['./easyrsa', 'gen-req', client_name, 'nopass', 'batch'],
                                                   cwd=PATH_TO_EASY_RSA)
                    key_process.wait()
                    chown(path_to_full_key, user=OWNED_BY_USER, group=OWNED_BY_USER)
                    # copy(path_to_full_key, FINISHED_KEY_LOCATION)
                except Exception as exception:
                    print('Unable to generate key: {}'.format(exception))
                    return b"Failed to generate key", 500
            if not path.exists(path_to_full_cert):
                try:
                    sign_process = subprocess.Popen(['./easyrsa', 'sign-req', 'client', client_name, 'batch'],
                                                    cwd=PATH_TO_EASY_RSA)
                    sign_process.wait()
                    chown(path_to_full_cert, user=OWNED_BY_USER, group=OWNED_BY_USER)
                    # copy(path_to_full_cert, FINISHED_KEY_LOCATION)
                except Exception as exception:
                    print('Unable to sign request: {}'.format(exception))
                    return b"Failed to sign request", 500
            with open(PATH_TO_BASE_OPENVPN_CONFIG, 'r') as base_config, open(PATH_TO_OPENVPN_CA, 'r') as base_ca, open(
                    path_to_full_cert) as full_cert, open(path_to_full_key) as full_key, open(
                PATH_TO_OPENVPN_TA_KEY, 'r') as base_ta, open(path_to_output_openvpn_config, 'w') as output_config:
                output_config.write(base_config.read())
                output_config.write('\n<ca>\n')
                output_config.write(base_ca.read())
                output_config.write('</ca>\n\n<cert>\n')
                output_config.write(full_cert.read())
                output_config.write('</cert>\n\n<key>\n')
                output_config.write(full_key.read())
                output_config.write('</key>\n\n<tls-auth>\n')
                output_config.write(base_ta.read())
                output_config.write('</tls-auth>\n\n')
        with open(path_to_client_config, 'w') as client_config:
            # We add 1 because for some unknown reason adding 13.0.16.0/20 to iptables auto-resolves the DNS
            starting_ip_address = GROW_STARTING_NETWORK + (grow_server_id + 1) * NUMBER_OF_SUBNETS
            # We will likely cache the iptables result/creation process but for now it's here
            should_create_iptables_entry = True
            rule_comment = "grow-{}".format(grow_identifier)
            table = iptc.Table(iptc.Table.FILTER)
            chain = iptc.Chain(table, "FORWARD")
            for rule in chain.rules:
                for match in rule.matches:
                    if match.name == 'comment' and str(match.comment) == str(rule_comment):
                        should_create_iptables_entry = False
                        break
                if not should_create_iptables_entry:
                    break
            if should_create_iptables_entry:
                source_and_destination = "{}/{}".format(starting_ip_address, GROW_NETMASK)
                subprocess.Popen(
                    'iptables -I FORWARD -s {} -d {} --jump ACCEPT --protocol all -m comment --comment "{}"'.format(
                        source_and_destination, source_and_destination, rule_comment), shell=True)
            # If the client_type is an administrator or core we always reserve the first two
            # ip addresses. Otherwise we increment up to the limit for this grow's subnet
            if client_type == ALLOWED_CLIENT_TYPES[0]:
                ip_address_incrementor = 1
            elif client_type == ALLOWED_CLIENT_TYPES[1]:
                ip_address_incrementor = 2
            else:
                ip_address_incrementor = redis_client.incr(REDIS_KEY_GROW_CLIENT_COUNTER.format(grow_server_id))
                if ip_address_incrementor > NUMBER_OF_HOSTS:
                    return b"Exceeded the number of clients for this server", 429
            device_ip_address = str(starting_ip_address + ip_address_incrementor)
            client_config.write(
                'ifconfig-push {} {}\npush "route {} {}"\n'.format(device_ip_address, GROW_NETMASK, starting_ip_address,
                                                                   GROW_NETMASK))
        chown(path_to_client_config, user=OPENVPN_USER, group=OPENVPN_GROUP)
        with open(path_to_output_openvpn_config) as final_openvpn_config:
            return jsonify({"config": final_openvpn_config.read(),
                            "device": {"ip_address": device_ip_address, "name": client_name}})
    return b"Only POSTing allowed", 405
