"""DNS Authenticator using RFC 2136 Dynamic Updates."""
import logging

import dns.flags
import dns.message
import dns.name
import dns.query
import dns.rdataclass
import dns.rdatatype
import dns.tsig
import dns.tsigkeyring
import dns.update
import zope.interface

from certbot import errors
from certbot import interfaces
from certbot.plugins import dns_common

logger = logging.getLogger(__name__)


@zope.interface.implementer(interfaces.IAuthenticator)
@zope.interface.provider(interfaces.IPluginFactory)
class Authenticator(dns_common.DNSAuthenticator):
    """DNS Authenticator using RFC 2136 Dynamic Updates

    This Authenticator uses RFC 2136 Dynamic Updates to fulfull a dns-01 challenge.
    """

    description = 'Obtain certs using a DNS TXT record (if you are using BIND for DNS).'
    ttl = 120

    def __init__(self, *args, **kwargs):
        super(Authenticator, self).__init__(*args, **kwargs)
        self.credentials = None

    @classmethod
    def add_parser_arguments(cls, add):
        super(Authenticator, cls).add_parser_arguments(add)
        add('credentials', help='RFC 2136 credentials INI file.')

    def more_info(self):
        return 'This plugin configures a DNS TXT record to respond to a dns-01 challenge using ' + \
               'RFC 2136 Dynamic Updates.'

    def _setup_credentials(self):
        self.credentials = self._configure_credentials(
            'credentials',
            'RFC 2136 credentials INI file',
            {
                'name': 'TSIG key name',
                'secret': 'TSIG key secret',
                'algorithm': 'TSIG key algorithm',
                'server': 'The target DNS server'
            }
        )

    def _perform(self, domain, validation_name, validation):
        self._get_rfc2136_client().add_txt_record(domain, validation_name, validation, self.ttl)

    def _cleanup(self, domain, validation_name, validation):
        self._get_rfc2136_client().del_txt_record(domain, validation_name, validation)

    def _get_rfc2136_client(self):
        return _RFC2136Client(self.credentials.conf('server'), self.credentials.conf('name'), self.credentials.conf('secret'), self.credentials.conf('algorithm'))


class _RFC2136Client(object):
    """
    Encapsulates all communication with the target DNS server.
    """

    ALGORITHMS = {
      'HMAC-MD5': dns.tsig.HMAC_MD5,
      'HMAC-SHA1': dns.tsig.HMAC_SHA1,
      'HMAC-SHA224': dns.tsig.HMAC_SHA224,
      'HMAC-SHA256': dns.tsig.HMAC_SHA256,
      'HMAC-SHA384': dns.tsig.HMAC_SHA384,
      'HMAC-SHA512': dns.tsig.HMAC_SHA512
    }

    def __init__(self, server, key_name, key_secret, key_algorithm='HMAC-MD5'):
        self.server = server
        self.keyring = dns.tsigkeyring.from_text({
            key_name: key_secret
        })
        self.algorithm = self.ALGORITHMS.get(key_algorithm, dns.tsig.HMAC_MD5)

    def add_txt_record(self, domain_name, record_name, record_content, record_ttl):
        """
        Add a TXT record using the supplied information.

        :param str domain: The domain to use to find the closest SOA.
        :param str record_name: The record name (typically beginning with '_acme-challenge.').
        :param str record_content: The record content (typically the challenge validation).
        :param int record_ttl: The record TTL (number of seconds that the record may be cached).
        :raises certbot.errors.PluginError: if an error occurs communicating with the DNS server
        """

        domain = self._find_domain(domain_name)

        n = dns.name.from_text(record_name)
        o = dns.name.from_text(domain)
        rel = n.relativize(o)

        update = dns.update.Update(
            domain,
            keyring=self.keyring,
            keyalgorithm=self.algorithm)
        update.add(rel, record_ttl, dns.rdatatype.TXT, record_content)

        try:
            response = dns.query.tcp(update, self.server)
            rcode = response.rcode()

            if rcode == dns.rcode.NOERROR:
                logger.debug('Successfully added TXT record')
        except Exception as e:
            raise errors.PluginError('Encountered error adding TXT record: {0}'
                                     .format(e))

    def del_txt_record(self, domain_name, record_name, record_content):
        """
        Delete a TXT record using the supplied information.

        :param str domain: The domain to use to find the closest SOA.
        :param str record_name: The record name (typically beginning with '_acme-challenge.').
        :param str record_content: The record content (typically the challenge validation).
        :param int record_ttl: The record TTL (number of seconds that the record may be cached).
        :raises certbot.errors.PluginError: if an error occurs communicating with the DNS server
        """

        domain = self._find_domain(domain_name)

        n = dns.name.from_text(record_name)
        o = dns.name.from_text(domain)
        rel = n.relativize(o)

        update = dns.update.Update(
            domain,
            keyring=self.keyring,
            keyalgorithm=self.algorithm)
        update.delete(rel, dns.rdatatype.TXT, record_content)

        try:
            response = dns.query.tcp(update, self.server)
            rcode = response.rcode()

            if rcode == dns.rcode.NOERROR:
                logger.debug('Successfully deleted TXT record')
        except Exception as e:
            raise errors.PluginError('Encountered error deleting TXT record: {0}'
                                     .format(e))

    def _find_domain(self, domain_name):
        """
        Find the closest domain with an SOA record for a given domain name.

        :param str domain_name: The domain name for which to find the closest SOA record.
        :returns: The domain, if found.
        :rtype: str
        :raises certbot.errors.PluginError: if no SOA record can be found.
        """

        domain_name_guesses = dns_common.base_domain_name_guesses(domain_name)

        # Loop through until we find an authoritative SOA record
        for guess in domain_name_guesses:
            domain = dns.name.from_text(guess)
            if not domain.is_absolute():
                domain = domain.concatenate(dns.name.root)

            request = dns.message.make_query(domain, dns.rdatatype.SOA, dns.rdataclass.IN)
            # Turn off Recursion Desired bit in query
            request.flags ^= dns.flags.RD

            try:
                response = dns.query.udp(request, self.server)
                rcode = response.rcode()

                # Authoritative Answer bit should be set
                if rcode == dns.rcode.NOERROR and len(response.answer) > 0 and response.flags & dns.flags.AA:
                    logger.debug('Received authoritative SOA response for {0}'.format(guess))
                    return guess

                logger.debug('No authoritative SOA record found for {0}'.format(guess))
            except Exception as e:
                raise errors.PluginError('Encountered error when making query: {0}'
                                         .format(e))

        raise errors.PluginError('Unable to determine base domain for {0} using names: {1}.'
                                 .format(domain_name, domain_name_guesses))