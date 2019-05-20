import threading
import ipaddress
import urllib
import queue
import json
import time
import os

import requests

RESERVED = [
    ipaddress.IPv4Network('0.0.0.0/8'),
    ipaddress.IPv4Network('10.0.0.0/8'),
    ipaddress.IPv4Network('100.64.0.0/10'),
    ipaddress.IPv4Network('127.0.0.0/8'),
    ipaddress.IPv4Network('169.254.0.0/16'),
    ipaddress.IPv4Network('172.16.0.0/12'),
    ipaddress.IPv4Network('192.0.0.0/24'),
    ipaddress.IPv4Network('192.0.2.0/24'),
    ipaddress.IPv4Network('192.88.99.0/24'),
    ipaddress.IPv4Network('192.168.0.0/16'),
    ipaddress.IPv4Network('198.18.0.0/15'),
    ipaddress.IPv4Network('198.51.100.0/24'),
    ipaddress.IPv4Network('203.0.113.0/24'),
    ipaddress.IPv4Network('224.0.0.0/4'),
    ipaddress.IPv4Network('240.0.0.0/4'),
    ipaddress.IPv4Network('255.255.255.255/32')
    ]


class DNSCheckerWorker(threading.Thread):
    def __init__(self, session, servers, request_queue, response_queue,
                 request_type=1):
        threading.Thread.__init__(self)
        self.session = session
        self.servers = servers
        self.request_queue = request_queue
        self.response_queue = response_queue
        self.request_type = request_type
    def run(self):
        session = self.session
        servers = self.servers
        request_queue = self.request_queue
        response_queue = self.response_queue
        pos = 0
        try:
            while True:
                old_domain = domain = request_queue.get_nowait()
                if self.request_type == 12:
                    try:
                        ip = ipaddress.ip_address(domain)
                    except ValueError:
                        response_queue.put((old_domain, {}))
                        continue
                    if isinstance(ip, ipaddress.IPv4Address):
                        domain = ip.exploded.split('.')[::-1]
                    else:
                        domain = ipaddress.ip_address(domain).exploded.replace(':', '')[::-1]
                    domain = '.'.join(domain)
                    domain += '.in-addr.arpa.'
                success = False
                for retry in range(3):
                    pos = (pos + 1) % len(servers)
                    server = servers[pos]
                    try:
                        r = session.get(server + urllib.parse.urlencode(
                            {'name':domain, 'type':self.request_type}))
                        response_queue.put((old_domain, r.json()))
                        if retry > 0:
                            print('Fixed\n', end='')
                        success = True
                        break
                    except Exception as error:
                        print(server+'\n', end='')
                if not success:
                    print('Could\'t fix\n', end='')
                    response_queue.put((domain, True))
        except queue.Empty:
            pass
class ArgusPassiveDNS(threading.Thread):
    def __init__(self, session, request_queue, response_queue):
        threading.Thread.__init__(self)
        self.session = session
        self.url = 'https://api.mnemonic.no/pdns/v3/search'
        self.data = {'aggregateResult':True, 'customerID':[], 'includeAnonymousResults':True,
                'limit':1000**2,'offset':0,'rrClass':[], 'tlp':[]}
        self.request_queue = request_queue
        self.response_queue = response_queue
    def run(self):
        request_queue = self.request_queue
        response_queue = self.response_queue
        try:
            while True:
                ip = request_queue.get_nowait()
                self.data['query'] = ip
                domains = set()
                for _ in range(3):
                    try:
                        r = self.session.post(self.url, json=self.data)
                        if r.status_code == 503:
                            time.sleep(5)
                            continue
                        for item in r.json()['data']:
                            domains.add(item['query'])
                        response_queue.put((ip, domains))
                        print(ip, len(domains))
                        break
                    except requests.exceptions.ConnectionError:
                        time.sleep(1)
                    except TypeError:
                        print(r.json())
                        try:
                            time.sleep(r.json()['metaData']['millisUntilResourcesAvailable']/1000)
                        except Exception as error:
                            print(error)
                            time.sleep(1)
                        break
                    except json.decoder.JSONDecodeError:
                        time.sleep(1)
                        break
                    except Exception as error:
                        print(error.with_traceback(None))
                        time.sleep(1)
                        break
                    if _ == 2:
                        print('Failed 3 times')
        except queue.Empty:
            pass



class DNSChecker():
    def __init__(self):
        self.session = requests.Session()
        self.session.headers['User-Agent'] = 'DOH'
        self.session.headers['Accept'] = 'application/dns-json'
        self.session2 = requests.Session()
        self.session2.headers['User-Agent'] = 'DOH'
        self.session2.headers['Accept'] = '*/*'
        self.servers = ['https://dns.google.com/resolve?',
                        'https://cloudflare-dns.com/dns-query?',
                        'https://doh.securedns.eu/dns-query?',]
        self.cache = dict()
        self.reverse_cache = dict()
        one_week_ago = (time.time() - 60*60*24*7)
        try:
            with open('dns_cache.txt') as file:
                for line in file:
                    try:
                        domain, ip, last_modified = line.rstrip().split(',')
                        last_modified = int(last_modified)
                        if last_modified > one_week_ago:
                            self.cache[domain] = (ip, last_modified)
                    except (ValueError):
                        pass
        except FileNotFoundError:
            pass
        try:
            with open('reverse_dns_cache.txt') as file:
                for line in file:
                    try:
                        ip_address, last_modified, *domains = line.rstrip().split(',')
                        last_modified = int(last_modified)
                        if last_modified > one_week_ago:
                            self.reverse_cache[ip_address] = (last_modified, domains)
                    except (ValueError):
                        pass
        except FileNotFoundError:
            pass
    def mass_check(self, domain_list, thread_count=40):
        domain_list_length = len(domain_list)
        cache = self.cache
        request_queue = queue.Queue()
        response_queue = queue.Queue()
        results = dict()
        all_from_cache = True
        valid_count = 0
        invalid_count = 0
        for domain in domain_list:
            try:
                results[domain] = cache[domain][0]
                if cache[domain][0]:
                    valid_count += 1
                else:
                    invalid_count += 1
            except KeyError:
                request_queue.put(domain)
                all_from_cache = False
        if all_from_cache:
            return results
        del domain_list
        threads = []
        for i in range(thread_count):
            thread = DNSCheckerWorker(self.session, self.servers, request_queue,
                                      response_queue, 1)
            thread.start()
            threads.append(thread)
        start = time.time()
        initial_length = len(results)
        while any(thread.is_alive() for thread in threads):
            try:
                while True:
                    domain, result = response_queue.get(timeout=0.1)
                    exists = result['Status'] == 0 and 'Answer' in result
                    ip = ''
                    if exists:
                        for answer in result['Answer']:
                            try:
                                ip = ipaddress.IPv4Network('%s/32' % answer['data'])
                                exists = exists and (not any(network.overlaps(ip) for network in RESERVED))
                            except ipaddress.AddressValueError:
                                pass
                    if exists and isinstance(ip, ipaddress.IPv4Network):
                        valid_count += 1
                        ip = ip.network_address.exploded
                    else:
                        invalid_count += 1
                        ip = ''
                    results[domain] = ip
                    cache[domain] = (ip, time.time())
                    if len(results) % 2000 == 1999:
                        print('%s/s %s Valid: %s Invalid: %s ' % (round((len(results) - initial_length)/(time.time() - start), 2),
                              round(len(results)/domain_list_length, 5), valid_count, invalid_count))
                        lines = [[i, cache[i][0], str(int(cache[i][1]))] for i in sorted(cache)]
                        with open('temp', 'w') as file:
                            file.write('\n'.join(','.join(line) for line in lines))
                        os.replace('temp', 'dns_cache.txt')
            except queue.Empty:
                pass
            except Exception as error:
                print(error)
                print(domain, result)
        lines = [[i, cache[i][0], str(int(cache[i][1]))] for i in sorted(cache)]
        with open('temp', 'w') as file:
            file.write('\n'.join(','.join(line) for line in lines))
        os.replace('temp', 'dns_cache.txt')
        return results
    def mass_reverse_lookup(self, ip_list, thread_count=40):
        domain_list_length = len(ip_list)
        reverse_cache = self.reverse_cache
        request_queue = queue.Queue()
        response_queue = queue.Queue()
        request_queue2 = queue.Queue()
        response_queue2 = queue.Queue()
        results = set()
        all_from_cache = True
        for ip in ip_list:
            try:
                results.update(reverse_cache[ip][1])
            except KeyError:
                request_queue.put(ip)
                request_queue2.put(ip)
                all_from_cache = False
        if not all_from_cache:
            threads = []
            for i in range(thread_count):
                thread = DNSCheckerWorker(self.session, self.servers, request_queue,
                                          response_queue, 12)
                thread.start()
                threads.append(thread)
            for i in range(10):
                thread = ArgusPassiveDNS(self.session2, request_queue2, response_queue2)
                thread.start()
                threads.append(thread)
            while any(thread.is_alive() for thread in threads):
                try:
                    while True:
                        ip, result = response_queue.get(timeout=0.1)
                        try:
                            if result['Status'] == 0:
                                domains = [answer['data'].lower().strip('.') for answer in result['Answer'] if answer['type'] == 12]
                                reverse_cache[ip] = [time.time(), domains]
                                results.update(domains)
                            else:
                                reverse_cache[ip] = (time.time(), [])
                        except KeyError:
                            reverse_cache[ip] = (time.time(), [])
                except queue.Empty:
                    pass
                try:
                    while True:
                        ip, result = response_queue2.get(timeout=0.1)
                        result = list(result)
                        try:
                            reverse_cache[ip][1].extend(result)
                        except KeyError:
                            reverse_cache[ip] = [time.time(), result]
                        results.update(result)
                except queue.Empty:
                    pass
        print('Fetched/Loaded from cache domains')
        domain_to_ip = self.mass_check(results, thread_count)
        print('Fetched/Loaded from cache ip addresses')
        domain_to_ip_dict = {'1':set()}
        for domain in domain_to_ip:
            now = domain_to_ip_dict.get(domain_to_ip[domain], set())
            now.add(domain)
            domain_to_ip_dict[domain_to_ip[domain]] = now
        print('Generated domain_to_ip_dict')
        for ip in domain_to_ip_dict:
            if ip in reverse_cache:
                old = reverse_cache[ip][1]
                new = set(old)
                new.update(domain_to_ip_dict[ip])
                keep = set()
                keep.update(domain_to_ip_dict[ip])
                keep.update(domain_to_ip_dict['1'])
                new.intersection_update(keep)
                new = list(new)
                reverse_cache[ip][1].clear()
                reverse_cache[ip][1].extend(new)
        print('Started with %s rules' % len(results))
        results.clear()
        results.update(domain_to_ip_dict['1'])
        for ip in ip_list:
            try:
                results.update(domain_to_ip_dict[ip])
            except KeyError:
                pass
        print('Removed domains that resolve no longer resolve to mentioned address: %s' % len(results))
        lines = [','.join((ip, str(int(reverse_cache[ip][0])), *sorted(list(set(reverse_cache[ip][1])))))
                 for ip in sorted(reverse_cache)]
        with open('temp', 'w') as file:
            file.write('\n'.join(lines))
        os.replace('temp', 'reverse_dns_cache.txt')
        return results

