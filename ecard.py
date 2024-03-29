#!/usr/bin/python3

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

import lxml.html as html_parser
import requests
from requests import Response

__version__ = '2.2.0'

# ----- CONFIGURATION -----
# Bank's name is defined in the url of the e-cartebleue service.
# It could be caisse-epargne, sg, labanquepostale, banquepopulaire, banquebcp...
bank = 'caisse-epargne'

# gopass keys
login_gopass_location = 'me/sites/e-cartebleue.com/{card} user'
password_gopass_location = 'me/sites/e-cartebleue.com/{card}'
default_card = 'joint'
# --- END CONFIGURATION ---

# global vars
t3ds_host = 'https://natixispaymentsolutions-3ds-vdm.wlp-acs.com'


class ECard:
    def __init__(self, number, expired_at, cvv, owner):
        self.number = number
        self.expired_at = expired_at
        self.cvv = cvv
        self.owner = owner

    def __str__(self):
        return 'Card number : ' + str(self.number) \
               + '\nExpired at  : ' + str(self.expired_at) \
               + '\nCVV         : ' + str(self.cvv) \
               + '\nOwner       : ' + str(self.owner)


class TableFormatter:
    def __init__(self):
        self.rows = []
        self.rows_length = {}

    def set_rows(self, rows: list):
        self.rows = rows
        for row in rows:
            for num, value in enumerate(row, start=0):
                if not self.rows_length.__contains__(num) or self.rows_length[num] < len(value):
                    self.rows_length[num] = len(value)

    def format_value(self, num, value):
        return ' ' * (self.rows_length[num] - len(value)) + value

    def generate_separator(self, start, middle, end):
        result = start
        for length in self.rows_length.values():
            result = result + '─' * (length + 2) + middle
        return result[:-1] + end

    def __str__(self):
        result = self.generate_separator('╭', '┬', '╮') + '\n'
        header = True
        for row in self.rows:
            result_row = ''
            for num, value in enumerate(row, start=0):
                result_row = result_row + self.format_value(num, value) + ' │ '
            result = result + '│ ' + result_row + '\n'
            if header:
                result = result + self.generate_separator('├', '┼', '┤') + '\n'
                header = False

        return result + self.generate_separator('╰', '┴', '╯')


class ECardManager:
    def __init__(self):
        self.host = 'https://service.e-cartebleue.com/fr/' + bank
        self.token = None
        self.jsessionid = None

        self.auth_3ds_needed = None
        self.auth_3ds_md = None
        self.auth_3ds_pareq = None
        self.auth_3ds_termurl = None

    def do_login(self, login, password):
        logger.debug('HEADER LOGIN')

        headers = ECardManager.get_common_headers({})
        payload = {
            'request': 'login',
            'identifiantCrypte': '',
            'app': '',
            'identifiant': login,
            'memorize': 'false',
            'password': password,
            'token': '9876543210'
        }
        response = ECardManager._post_form(self.host + '/login', headers, payload)
        dom = html_parser.document_fromstring(response.text)
        ECardManager.check_error(dom)

        logger.debug('\n# LoginInfo')

        # get jsessionid
        self.jsessionid = response.cookies['JSESSIONID']
        logger.debug('jsessionid: ' + self.jsessionid)

        # get token
        self.token = dom.xpath('//input[@name="token"]')[0].attrib['value'].strip()
        logger.debug('token: ' + self.token)

        # check if D secure is needed
        auth_3ds_form = dom.xpath('//form[@id="form-3ds-authentificate"]')
        self.auth_3ds_needed = len(auth_3ds_form) > 0
        logger.debug('need3dsecure: ' + str(self.auth_3ds_needed))

        if self.auth_3ds_needed:
            self.auth_3ds_md = dom.xpath('//input[@name="MD"]')[0].attrib['value'].strip()
            self.auth_3ds_pareq = dom.xpath('//input[@name="PaReq"]')[0].attrib['value'].strip()
            self.auth_3ds_termurl = dom.xpath('//input[@name="TermUrl"]')[0].attrib['value'].strip()

        return True

    def auth_3ds(self):
        print('3D Secure authentication required. Loading...')

        # 1.1 PaRequest...
        url = t3ds_host + '/acs-pa-service/pa/paRequest'
        headers = ECardManager.get_common_headers({})
        payload = {
            'MD': self.auth_3ds_md,
            'PaReq': self.auth_3ds_pareq,
            'TermUrl': self.host + '/receive3ds'
        }
        response = ECardManager._post_form(url, headers, payload, allow_redirects=False)
        redirect_url = response.headers['Location']
        logger.debug('##### redirect url\n' + redirect_url)

        index = redirect_url.rfind('/')
        auth_3ds_id = redirect_url[index + 1:]
        logger.debug('##### auth 3ds id\n' + auth_3ds_id)

        # 1.2 ...do the redirection
        headers = ECardManager.get_common_headers({})
        ECardManager._get(redirect_url, headers)

        # 2. get session
        url = t3ds_host + '/acs-auth-pages/authent/pages/getSession/' + auth_3ds_id
        headers = ECardManager.get_common_headers({})
        payload = {
            'inIframe': False,
            'parentUrl': None
        }
        response = ECardManager._post_json(url, headers, payload)
        account_id = json.loads(response.text)['accountId']
        transaction_id = json.loads(response.text)['hubSessionId']
        logger.debug('##### account id\n' + account_id)

        # 3. start authentication
        url = t3ds_host + '/acs-auth-pages/authent/pages/startAuthent'
        payload = {
            'accountId': account_id,
            'language': 'fr',
            'region': 'FR',
            'hubAuthenticationInput': {
                'transactionContext': {}
            }
        }
        response = ECardManager._post_json(url, headers, payload)

        # Check authentication type: OTP_SMS or MOBILE_APP
        means_to_use = json.loads(response.text)['meansToUse']
        if means_to_use == 'OTP_SMS':
            self.auth_by_otp_sms(headers, account_id)
        elif means_to_use == 'MOBILE_APP':
            auth_id = json.loads(response.text)['hubAuthenticationOutput']['id']
            self.auth_by_mobile_app(headers, account_id, transaction_id, auth_id)
        else:
            print('Unknown authentication mode: ' + means_to_use)
            return
        self.auth_end(headers, account_id)

    def auth_by_otp_sms(self, headers, account_id):
        # 4.1 ask for OTP_SMS code
        print('Authentication by SMS')
        otp_code = input('Enter code: ')

        # 4.2 update authentication with OTP code
        url = t3ds_host + '/acs-auth-pages/authent/pages/updateAuthent'
        payload = {
            'accountId': account_id,
            'language': 'fr',
            'step': 'otp_validating_3',
            'skipCurrentHubCall': False,
            'hubAuthenticationInput': {
                'otp': otp_code,
                'merchantWhitelistedByUser': False
            }
        }
        response = ECardManager._post_json(url, headers, payload)
        if json.loads(response.text)['hubAuthenticationOutput']['authenticationSuccess'] is False:
            raise Exception('\n\033[91m/!\\ AUTHENTICATION ERROR /!\\\033[0m\nWrong authentication code.')

    def auth_by_mobile_app(self, headers, account_id, transaction_id, auth_id):
        # 4.1 polling for success
        print('Authentication by mobile')
        print('Waiting for auth...')

        url = t3ds_host + '/acs-auth-pages/authent/pages/startPolling'
        payload = {
            'accountId': account_id,
            'hubAuthenticationInput': {
                'authenticationId': auth_id,
                'transactionId': transaction_id
            }
        }
        # continue_polling = True
        while True:
            time.sleep(5)
            response = ECardManager._post_json(url, headers, payload)
            auth_success = json.loads(response.text)['hubAuthenticationOutput']['authenticationSuccess']
            auth_canceled = json.loads(response.text)['hubAuthenticationOutput']['authenticationCanceled']
            auth_blocked = json.loads(response.text)['hubAuthenticationOutput']['authenticationBlocked']
            auth_failed = json.loads(response.text)['hubAuthenticationOutput']['authenticationFailed']
            auth_timeout = json.loads(response.text)['hubAuthenticationOutput']['authenticationTimeOut']

            if auth_success:
                print('Authentication succeeded')
                break

            if auth_canceled:
                raise Exception('\n\033[91m/!\\ AUTHENTICATION ERROR /!\\\033[0m\nAuthentication canceled.')
            if auth_blocked:
                raise Exception('\n\033[91m/!\\ AUTHENTICATION ERROR /!\\\033[0m\nAuthentication blocked.')
            if auth_failed:
                raise Exception('\n\033[91m/!\\ AUTHENTICATION ERROR /!\\\033[0m\nAuthentication failed.')
            if auth_timeout:
                raise Exception('\n\033[91m/!\\ AUTHENTICATION ERROR /!\\\033[0m\nAuthentication time out.')

    def auth_end(self, headers, account_id):
        # 5. end authentication
        url = t3ds_host + '/acs-auth-pages/authent/pages/endAuthent'
        payload = {
            'accountId': account_id,
            'hubAuthenticationInput': {}
        }
        ECardManager._post_json(url, headers, payload)

        # 6 get paResponse
        url = t3ds_host + '/acs-pa-service/pa/paRequestFromAuthPages'
        headers = ECardManager.get_common_headers({
            'Upgrade-Insecure-Requests': '1'
        })
        payload = {
            'accountId': account_id,
        }
        response = ECardManager._post_form(url, headers, payload)
        dom = html_parser.document_fromstring(response.text)
        md = dom.xpath('//input[@name="MD"]')[0].attrib['value'].strip()
        pares = dom.xpath('//input[@name="PaRes"]')[0].attrib['value'].strip()
        logger.debug('##### md\n' + md)
        logger.debug('##### PaResp\n' + pares)

        # finally, send the PaRes code to the bank
        url = self.host + '/receive3ds'
        headers = ECardManager.get_common_headers({
            'Cookie': 'JSESSIONID=' + self.jsessionid + '; eCarteBleue-pref=open',
            'Upgrade-Insecure-Requests': '1'
        })
        payload = {
            'MD': md,
            'PaRes': pares
        }
        response = ECardManager._post_form(url, headers, payload)
        dom = html_parser.document_fromstring(response.text)
        ECardManager.check_error(dom)

    def generate_ecard(self, amount: str, currency: str, validity: str) -> ECard:
        logger.debug('HEADER generate ecard')

        headers = ECardManager.get_common_headers({
            'Cookie': 'JSESSIONID=' + self.jsessionid + '; eCarteBleue-pref=open'
        })
        payload = {
            'request': 'ocode',
            'token': self.token,
            'montant': amount,
            'devise': currency,
            'dateValidite': validity
        }

        response = ECardManager._post_form(self.host + '/cpn', headers, payload)
        dom = html_parser.document_fromstring(response.text)
        ECardManager.check_error(dom)

        number = dom.xpath('//dd[@id="generated-code-dd"]/span[@data-drag-txt]')[0].attrib['data-drag-txt'].strip()
        expired_at = dom.xpath('//dl[@id="content-expiration-date"]/dd')[0].text.strip()
        cvv = dom.xpath('//dl[@id="content-cryptogramme"]//span[@class="restricted-only"]')[0].text.strip()
        owner = dom.xpath('//dl[@id="content-card-owner"]//span[@class="restricted-only"]')[0].text.strip()

        e_card = ECard(number, expired_at, cvv, owner)
        return e_card

    def list_historic(self):
        logger.debug('HEADER historic')

        headers = ECardManager.get_common_headers({
            'Cookie': 'JSESSIONID=' + self.jsessionid + '; eCarteBleue-pref=open'
        })
        payload = {
            'token': self.token,
        }

        response = ECardManager._post_form(self.host + '/historic', headers, payload)
        html = response.text

        dom = html_parser.document_fromstring(html)
        ECardManager.check_error(dom)

        used = dom.xpath('//div[@id="history-panes-used-numbers-print"]//table/tr')
        unused = dom.xpath('//div[@id="history-panes-unused-numbers-print"]//table/tr')

        items = []
        for row in used + unused:
            item = []
            cols = row.getchildren()
            # remove last column (status)
            cols.pop(5)
            for col in cols:
                value = col.text.strip()
                value = '─' if value == '-----------' else value
                item.append(value)
            items.append(item)

        # sort by date
        items.sort(key=lambda date: datetime.strptime(date[0], '%d/%m/%Y'), reverse=True)

        # add headers
        items.insert(0, ['DATE   ', 'COMMERCANT', 'E-NUMERO     ', 'PLAFOND', 'TRANSACTION'])
        return items

    def do_logout(self):
        logger.debug('HEADER logout')
        headers = ECardManager.get_common_headers({
            'Cookie': 'JSESSIONID=' + self.jsessionid + '; eCarteBleue-pref=open'
        })
        ECardManager._get(self.host + '/logout', headers=headers)

    @staticmethod
    def _post_form(url: str, headers: dict, payload: dict, allow_redirects=True) -> Response:
        headers.update({'Content-Type': 'application/x-www-form-urlencoded'})
        return ECardManager._post(url, headers, urllib.parse.urlencode(payload), allow_redirects)

    @staticmethod
    def _post_json(url: str, headers: dict, payload: dict, allow_redirects=True) -> Response:
        headers.update({'Content-Type': 'application/json'})
        return ECardManager._post(url, headers, json.dumps(payload), allow_redirects)

    @staticmethod
    def _post(url: str, headers: dict, payload: str, allow_redirects=True) -> Response:
        response = requests.post(url, headers=headers, data=payload, allow_redirects=allow_redirects)
        ECardManager._process_response(response)
        return response

    @staticmethod
    def _get(url: str, headers: dict, allow_redirects=True) -> Response:
        response = requests.get(url, headers=headers, allow_redirects=allow_redirects)
        ECardManager._process_response(response)
        return response

    @staticmethod
    def _process_response(response: Response):
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug('HEADER REQUESTS')
            logger.debug('\n##### url\nPOST ' + response.url)
            logger.debug('\n##### request headers\n' + str(response.request.headers))
            logger.debug('\n##### request body\n' + str(response.request.body))
            logger.debug('\n##### response code\n' + str(response.status_code))
            logger.debug('\n##### response headers\n' + str(response.headers))
            # remove empty lines
            text = os.linesep.join([s for s in response.text.splitlines() if s.strip()])
            text = text.replace('\t', '  ')
            logger.debug('\n# response body\n' + text)

        if response.status_code >= 400:
            raise Exception(
                '\n\033[91m/!\\ ERROR /!\\\033[0m\nSomething went wrong when calling ' + response.url + '.\n'
                + str(response))

    @staticmethod
    def get_common_headers(extra_headers: dict) -> dict:
        headers = {
            'User-Agent': 'ecartebleue-python/' + __version__,
            'Accept': '*/*'
        }
        headers.update(extra_headers)
        return headers

    @staticmethod
    def check_error(dom: html_parser) -> None:
        errors = dom.xpath('//form[@id="form-error-confirmation"]//p[@role="alert"]')
        if len(errors) > 0:
            # convert <br> to \n
            for br in errors[0].xpath('//br'):
                br.tail = '\n' + br.tail if br.tail else '\n'
            raise Exception(errors[0].text_content().strip())


class ColourFilter(logging.Filter):
    colours = {'DEBUG': '\033[32m',
               'INFO': '\033[34m',
               'WARNING': '\033[93m',
               'ERROR': '\033[91m',
               'CRITICAL': '\033[4m\033[1m\033[91m'}

    def filter(self, record):
        msg = record.msg.replace('HEADER', '\n########################## ')
        record.msg = ColourFilter.colours[record.levelname] + msg + '\033[0m'
        return True


class ChoicesFormatter(argparse.RawTextHelpFormatter):
    def _format_action_invocation(self, action):
        return super(ChoicesFormatter, self)._format_action_invocation(action).replace(' ,', ',')


# run bash command
def bash(bash_command):
    try:
        process = subprocess.run(bash_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
        error = process.stderr.decode('utf-8').strip()
        if error:
            sys.stderr.write(error)
            sys.exit(1)
        return process.stdout.decode('utf-8').strip().split('\n', 1)[0]
    except subprocess.CalledProcessError:
        print("error run command: " + bash_command)
        sys.exit(1)


def amount_type(x):
    if float(x) <= 0.0:
        raise argparse.ArgumentTypeError("amount must be greater than 0")
    return x


def action_generate(args, e_card_manager: ECardManager):
    # params
    logger.debug('expire-in: ' + args.expire_in)
    logger.debug('amount: ' + args.amount)

    e_card = e_card_manager.generate_ecard(args.amount, '1.000000', args.expire_in)
    print('\n' + str(e_card) + '\n')


class ActionHistoric(argparse.Action):

    def __call__(self, _parser, namespace, values, option_string=None):
        run(namespace, ActionHistoric.do_action)
        # _parser.exit()

    @staticmethod
    def do_action(args, e_card_manager: ECardManager):
        logger.debug('HEADER historic')
        all_historic = e_card_manager.list_historic()
        table_formatter = TableFormatter()
        table_formatter.set_rows(all_historic)
        print(table_formatter)


def run(args, action):
    # set logger level
    level = logging.DEBUG if args.verbose else logging.INFO
    logger.setLevel(level)

    # gopass
    login = bash('gopass ' + login_gopass_location.format(card=args.card))
    logger.debug('login: ' + login)

    password = bash('gopass ' + password_gopass_location.format(card=args.card))
    logger.debug('password: ')

    e_card_manager = ECardManager()
    try:
        # login
        e_card_manager.do_login(login, password)

        # 3D Secure authentication, if needed
        if e_card_manager.auth_3ds_needed:
            e_card_manager.auth_3ds()

        # run the action
        action(args, e_card_manager)

    except Exception as e:
        print(e)
    finally:
        e_card_manager.do_logout()
        sys.exit(1)


# logger
logging.basicConfig(format='%(message)s')
logger = logging.getLogger('ecard')
logger.addFilter(ColourFilter())

# MAIN
if __name__ == '__main__':
    # arguments
    expire_in = ['3', '6', '9', '12', '15', '18', '21', '24']
    parser = argparse.ArgumentParser(formatter_class=ChoicesFormatter)
    parser.add_argument('amount', type=amount_type, help='amount in euro')
    parser.add_argument('-c', '--card', default=default_card, help='card''s name defined in gopass')
    parser.add_argument('-e', '--expire-in', choices=expire_in, default='3', metavar='',
                        help='expiration time in months, default is 3\nallowed values are ' + ', '.join(
                            expire_in) + '.')
    parser.add_argument('-l', '--list', action=ActionHistoric, nargs=0, help='list historic of generated e-Carte Bleue')
    parser.add_argument('-v', '--verbose', action='store_true', default=False, help='verbose mode')
    parser.add_argument('-V', '--version', action='version', version=__version__, help='display version and quit')
    _args = parser.parse_args()

    # launch default action
    run(_args, action_generate)
