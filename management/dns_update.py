#!/usr/bin/python3

# Creates DNS zone files for all of the domains of all of the mail users
# and mail aliases and restarts nsd.
########################################################################

import os, os.path, urllib.parse, datetime, re, hashlib, base64
import ipaddress
import rtyaml

from mailconfig import get_mail_domains
from utils import shell, load_env_vars_from_file, safe_domain_name, sort_domains

def get_dns_domains(env):
	# Add all domain names in use by email users and mail aliases and ensure
	# PRIMARY_HOSTNAME is in the list.
	domains = set()
	domains |= get_mail_domains(env)
	domains.add(env['PRIMARY_HOSTNAME'])
	return domains

def get_dns_zones(env):
	# What domains should we create DNS zones for? Never create a zone for
	# a domain & a subdomain of that domain.
	domains = get_dns_domains(env)
	
	# Exclude domains that are subdomains of other domains we know. Proceed
	# by looking at shorter domains first.
	zone_domains = set()
	for domain in sorted(domains, key=lambda d : len(d)):
		for d in zone_domains:
			if domain.endswith("." + d):
				# We found a parent domain already in the list.
				break
		else:
			# 'break' did not occur: there is no parent domain.
			zone_domains.add(domain)

	# Make a nice and safe filename for each domain.
	zonefiles = []
	for domain in zone_domains:
		zonefiles.append([domain, safe_domain_name(domain) + ".txt"])

	# Sort the list so that the order is nice and so that nsd.conf has a
	# stable order so we don't rewrite the file & restart the service
	# meaninglessly.
	zone_order = sort_domains([ zone[0] for zone in zonefiles ], env)
	zonefiles.sort(key = lambda zone : zone_order.index(zone[0]) )

	return zonefiles
	
def get_custom_dns_config(env):
	try:
		return rtyaml.load(open(os.path.join(env['STORAGE_ROOT'], 'dns/custom.yaml')))
	except:
		return { }

def do_dns_update(env, force=False):
	# What domains (and their zone filenames) should we build?
	domains = get_dns_domains(env)
	zonefiles = get_dns_zones(env)

	# Custom records to add to zones.
	additional_records = get_custom_dns_config(env)

	# Write zone files.
	os.makedirs('/etc/nsd/zones', exist_ok=True)
	updated_domains = []
	for i, (domain, zonefile) in enumerate(zonefiles):
		# Build the records to put in the zone.
		records = build_zone(domain, domains, additional_records, env)

		# See if the zone has changed, and if so update the serial number
		# and write the zone file.
		if not write_nsd_zone(domain, "/etc/nsd/zones/" + zonefile, records, env, force):
			# Zone was not updated. There were no changes.
			continue

		# If this is a .justtesting.email domain, then post the update.
		try:
			justtestingdotemail(domain, records)
		except:
			# Hmm. Might be a network issue. If we stop now, will we end
			# up in an inconsistent state? Let's just continue.
			pass

		# Mark that we just updated this domain.
		updated_domains.append(domain)

		# Sign the zone.
		#
		# Every time we sign the zone we get a new result, which means
		# we can't sign a zone without bumping the zone's serial number.
		# Thus we only sign a zone if write_nsd_zone returned True
		# indicating the zone changed, and thus it got a new serial number.
		# write_nsd_zone is smart enough to check if a zone's signature
		# is nearing expiration and if so it'll bump the serial number
		# and return True so we get a chance to re-sign it.
		sign_zone(domain, zonefile, env)

	# Now that all zones are signed (some might not have changed and so didn't
	# just get signed now, but were before) update the zone filename so nsd.conf
	# uses the signed file.
	for i in range(len(zonefiles)):
		zonefiles[i][1] += ".signed"

	# Write the main nsd.conf file.
	if write_nsd_conf(zonefiles, env):
		# Make sure updated_domains contains *something* if we wrote an updated
		# nsd.conf so that we know to restart nsd.
		if len(updated_domains) == 0:
			updated_domains.append("DNS configuration")

	# Kick nsd if anything changed.
	if len(updated_domains) > 0:
		shell('check_call', ["/usr/sbin/service", "nsd", "restart"])

	# Write the OpenDKIM configuration tables.
	if write_opendkim_tables(zonefiles, env):
		# Settings changed. Kick opendkim.
		shell('check_call', ["/usr/sbin/service", "opendkim", "restart"])
		if len(updated_domains) == 0:
			# If this is the only thing that changed?
			updated_domains.append("OpenDKIM configuration")

	if len(updated_domains) == 0:
		# if nothing was updated (except maybe OpenDKIM's files), don't show any output
		return ""
	else:
		return "updated DNS: " + ",".join(updated_domains) + "\n"

########################################################################

def build_zone(domain, all_domains, additional_records, env, is_zone=True):
	records = []

	# For top-level zones, define ourselves as the authoritative name server.
	# 'False' in the tuple indicates these records would not be used if the zone
	# is managed outside of the box.
	if is_zone:
		records.append((None,  "NS",  "ns1.%s." % env["PRIMARY_HOSTNAME"], False))
		records.append((None,  "NS",  "ns2.%s." % env["PRIMARY_HOSTNAME"], False))

	# In PRIMARY_HOSTNAME...
	if domain == env["PRIMARY_HOSTNAME"]:
		# Define ns1 and ns2.
		# 'False' in the tuple indicates these records would not be used if the zone
		# is managed outside of the box.
		records.append(("ns1", "A", env["PUBLIC_IP"], False))
		records.append(("ns2", "A", env["PUBLIC_IP"], False))
		if env.get('PUBLIC_IPV6'):
			records.append(("ns1", "AAAA", env["PUBLIC_IPV6"], False))
			records.append(("ns2", "AAAA", env["PUBLIC_IPV6"], False))

		# Set the A/AAAA records. Do this early for the PRIMARY_HOSTNAME so that the user cannot override them
		# and we can provide different explanatory text.
		records.append((None, "A", env["PUBLIC_IP"], "Required. Sets the IP address of the box."))
		if env.get("PUBLIC_IPV6"): records.append((None, "AAAA", env["PUBLIC_IPV6"], "Required. Sets the IPv6 address of the box."))

		# Add a DANE TLSA record for SMTP.
		records.append(("_25._tcp", "TLSA", build_tlsa_record(env), "Recommended when DNSSEC is enabled. Advertises to mail servers connecting to the box that mandatory encryption should be used."))

		# Add a SSHFP records to help SSH key validation. One per available SSH key on this system.
		for value in build_sshfp_records():
			records.append((None, "SSHFP", value, "Optional. Provides an out-of-band method for verifying an SSH key before connecting. Use 'VerifyHostKeyDNS yes' (or 'VerifyHostKeyDNS ask') when connecting with ssh."))

	# The MX record says where email for the domain should be delivered: Here!
	records.append((None,  "MX",  "10 %s." % env["PRIMARY_HOSTNAME"], "Required. Specifies the hostname (and priority) of the machine that handles @%s mail." % domain))

	# SPF record: Permit the box ('mx', see above) to send mail on behalf of
	# the domain, and no one else.
	records.append((None,  "TXT", 'v=spf1 mx -all', "Recommended. Specifies that only the box is permitted to send @%s mail." % domain))

	# Add DNS records for any subdomains of this domain. We should not have a zone for
	# both a domain and one of its subdomains.
	subdomains = [d for d in all_domains if d.endswith("." + domain)]
	for subdomain in subdomains:
		subdomain_qname = subdomain[0:-len("." + domain)]
		subzone = build_zone(subdomain, [], {}, env, is_zone=False)
		for child_qname, child_rtype, child_value, child_explanation in subzone:
			if child_qname == None:
				child_qname = subdomain_qname
			else:
				child_qname += "." + subdomain_qname
			records.append((child_qname, child_rtype, child_value, child_explanation))

	def has_rec(qname, rtype, prefix=None):
		for rec in records:
			if rec[0] == qname and rec[1] == rtype and (prefix is None or rec[2].startswith(prefix)):
				return True
		return False

	# The user may set other records that don't conflict with our settings.
	for qname, rtype, value in get_custom_records(domain, additional_records, env):
		if has_rec(qname, rtype): continue
		records.append((qname, rtype, value, "(Set by user.)"))

	# Add defaults if not overridden by the user's custom settings (and not otherwise configured).
	defaults = [
		(None,  "A",    env["PUBLIC_IP"],       "Required. May have a different value. Sets the IP address that %s resolves to for web hosting and other services besides mail. The A record must be present but its value does not affect mail delivery." % domain),
		("www", "A",    env["PUBLIC_IP"],       "Optional. Sets the IP address that www.%s resolves to, e.g. for web hosting." % domain),
		(None,  "AAAA", env.get('PUBLIC_IPV6'), "Optional. Sets the IPv6 address that %s resolves to, e.g. for web hosting. (It is not necessary for receiving mail on this domain.)" % domain),
		("www", "AAAA", env.get('PUBLIC_IPV6'), "Optional. Sets the IPv6 address that www.%s resolves to, e.g. for web hosting." % domain),
	]
	for qname, rtype, value, explanation in defaults:
		if value is None or value.strip() == "": continue # skip IPV6 if not set
		if not is_zone and qname == "www": continue # don't create any default 'www' subdomains on what are themselves subdomains
		if not has_rec(qname, rtype):
			records.append((qname, rtype, value, explanation))

	# Append the DKIM TXT record to the zone as generated by OpenDKIM.
	opendkim_record_file = os.path.join(env['STORAGE_ROOT'], 'mail/dkim/mail.txt')
	with open(opendkim_record_file) as orf:
		m = re.match(r'(\S+)\s+IN\s+TXT\s+\( "([^"]+)"\s+"([^"]+)"\s*\)', orf.read(), re.S)
		val = m.group(2) + m.group(3)
		records.append((m.group(1), "TXT", val, "Recommended. Provides a way for recipients to verify that this machine sent @%s mail." % domain))

	# Append a DMARC record.
	records.append(("_dmarc", "TXT", 'v=DMARC1; p=quarantine', "Optional. Specifies that mail that does not originate from the box but claims to be from @%s is suspect and should be quarantined by the recipient's mail system." % domain))

	# For any subdomain with an A record but no SPF or DMARC record, add strict policy records.
	all_resolvable_qnames = set(r[0] for r in records if r[1] in ("A", "AAAA"))
	for qname in all_resolvable_qnames:
		if not has_rec(qname, "TXT", prefix="v=spf1 "):
			records.append((qname,  "TXT", 'v=spf1 a mx -all', "Prevents unauthorized use of this domain name for outbound mail by requiring outbound mail to originate from the indicated host(s)."))
		dmarc_qname = "_dmarc" + ("" if qname is None else "." + qname)
		if not has_rec(dmarc_qname, "TXT", prefix="v=DMARC1; "):
			records.append((dmarc_qname, "TXT", 'v=DMARC1; p=reject', "Prevents unauthorized use of this domain name for outbound mail by requiring a valid DKIM signature."))
		

	# Sort the records. The None records *must* go first in the nsd zone file. Otherwise it doesn't matter.
	records.sort(key = lambda rec : list(reversed(rec[0].split(".")) if rec[0] is not None else ""))

	return records

########################################################################

def get_custom_records(domain, additional_records, env):
	for qname, value in additional_records.items():
		# Is this record for the domain or one of its subdomains?
		if qname != domain and not qname.endswith("." + domain): continue

		# Turn the fully qualified domain name in the YAML file into
		# our short form (None => domain, or a relative QNAME).
		if qname == domain:
			qname = None
		else:
			qname = qname[0:len(qname)-len("." + domain)]

		# Short form. Mapping a domain name to a string is short-hand
		# for creating A records.
		if isinstance(value, str):
			values = [("A", value)]
			if value == "local" and env.get("PUBLIC_IPV6"):
				values.append( ("AAAA", value) )

		# A mapping creates multiple records.
		elif isinstance(value, dict):
			values = value.items()

		# No other type of data is allowed.
		else:
			raise ValueError()

		for rtype, value2 in values:
			# The "local" keyword on A/AAAA records are short-hand for our own IP.
			# This also flags for web configuration that the user wants a website here.
			if rtype == "A" and value2 == "local":
				value2 = env["PUBLIC_IP"]
			if rtype == "AAAA" and value2 == "local":
				if "PUBLIC_IPV6" not in env: continue # no IPv6 address is available so don't set anything
				value2 = env["PUBLIC_IPV6"]
			yield (qname, rtype, value2)

########################################################################

def build_tlsa_record(env):
	# A DANE TLSA record in DNS specifies that connections on a port
	# must use TLS and the certificate must match a particular certificate.
	#
	# Thanks to http://blog.huque.com/2012/10/dnssec-and-certificates.html
	# for explaining all of this!

	# Get the hex SHA256 of the DER-encoded server certificate:
	certder = shell("check_output", [
		"/usr/bin/openssl",
		"x509",
		"-in", os.path.join(env["STORAGE_ROOT"], "ssl", "ssl_certificate.pem"),
		"-outform", "DER"
		],
		return_bytes=True)
	certhash = hashlib.sha256(certder).hexdigest()

	# Specify the TLSA parameters:
	# 3: This is the certificate that the client should trust. No CA is needed.
	# 0: The whole certificate is matched.
	# 1: The certificate is SHA256'd here.
	return "3 0 1 " + certhash

def build_sshfp_records():
	# The SSHFP record is a way for us to embed this server's SSH public
	# key fingerprint into the DNS so that remote hosts have an out-of-band
	# method to confirm the fingerprint. See RFC 4255 and RFC 6594. This
	# depends on DNSSEC.
	#
	# On the client side, set SSH's VerifyHostKeyDNS option to 'ask' to
	# include this info in the key verification prompt or 'yes' to trust
	# the SSHFP record.
	#
	# See https://github.com/xelerance/sshfp for inspiriation.

	algorithm_number = {
		"ssh-rsa": 1,
		"ssh-dss": 2,
		"ecdsa-sha2-nistp256": 3,
	}

	# Get our local fingerprints by running ssh-keyscan. The output looks
	# like the known_hosts file: hostname, keytype, fingerprint.
	keys = shell("check_output", ["ssh-keyscan", "localhost"])
	for key in keys.split("\n"):
		if key.strip() == "" or key[0] == "#": continue
		try:
			host, keytype, pubkey = key.split(" ")
			yield "%d %d ( %s )" % (
				algorithm_number[keytype],
				2, # specifies we are using SHA-256 on next line
				hashlib.sha256(base64.b64decode(pubkey)).hexdigest().upper(),
				)
		except:
			# Lots of things can go wrong. Don't let it disturb the DNS
			# zone.
			pass
	
########################################################################

def write_nsd_zone(domain, zonefile, records, env, force):
	# On the $ORIGIN line, there's typically a ';' comment at the end explaining
	# what the $ORIGIN line does. Any further data after the domain confuses
	# ldns-signzone, however. It used to say '; default zone domain'.

	# The SOA contact address for all of the domains on this system is hostmaster
	# @ the PRIMARY_HOSTNAME. Hopefully that's legit.

	# For the refresh through TTL fields, a good reference is:
	# http://www.peerwisdom.org/2013/05/15/dns-understanding-the-soa-record/


	zone = """
$ORIGIN {domain}.
$TTL 1800           ; default time to live

@ IN SOA ns1.{primary_domain}. hostmaster.{primary_domain}. (
           __SERIAL__     ; serial number
           7200     ; Refresh (secondary nameserver update interval)
           1800     ; Retry (when refresh fails, how often to try again)
           1209600  ; Expire (when refresh fails, how long secondary nameserver will keep records around anyway)
           1800     ; Negative TTL (how long negative responses are cached)
           )
"""

	# Replace replacement strings.
	zone = zone.format(domain=domain, primary_domain=env["PRIMARY_HOSTNAME"])

	# Add records.
	for subdomain, querytype, value, explanation in records:
		if subdomain:
			zone += subdomain
		zone += "\tIN\t" + querytype + "\t"
		if querytype == "TXT":
			value = value.replace('\\', '\\\\') # escape backslashes
			value = value.replace('"', '\\"') # escape quotes
			value = '"' + value + '"' # wrap in quotes
		zone += value + "\n"

	# DNSSEC requires re-signing a zone periodically. That requires
	# bumping the serial number even if no other records have changed.
	# We don't see the DNSSEC records yet, so we have to figure out
	# if a re-signing is necessary so we can prematurely bump the
	# serial number.
	force_bump = False
	if not os.path.exists(zonefile + ".signed"):
		# No signed file yet. Shouldn't normally happen unless a box
		# is going from not using DNSSEC to using DNSSEC.
		force_bump = True
	else:
		# We've signed the domain. Check if we are close to the expiration
		# time of the signature. If so, we'll force a bump of the serial
		# number so we can re-sign it.
		with open(zonefile + ".signed") as f:
			signed_zone = f.read()
		expiration_times = re.findall(r"\sRRSIG\s+SOA\s+\d+\s+\d+\s\d+\s+(\d{14})", signed_zone)
		if len(expiration_times) == 0:
			# weird
			force_bump = True
		else:
			# All of the times should be the same, but if not choose the soonest.
			expiration_time = min(expiration_times)
			expiration_time = datetime.datetime.strptime(expiration_time, "%Y%m%d%H%M%S")
			if expiration_time - datetime.datetime.now() < datetime.timedelta(days=3):
				# We're within three days of the expiration, so bump serial & resign.
				force_bump = True

	# Set the serial number.
	serial = datetime.datetime.now().strftime("%Y%m%d00")
	if os.path.exists(zonefile):
		# If the zone already exists, is different, and has a later serial number,
		# increment the number.
		with open(zonefile) as f:
			existing_zone = f.read()
			m = re.search(r"(\d+)\s*;\s*serial number", existing_zone)
			if m:
				# Clear out the serial number in the existing zone file for the
				# purposes of seeing if anything *else* in the zone has changed.
				existing_serial = m.group(1)
				existing_zone = existing_zone.replace(m.group(0), "__SERIAL__     ; serial number")

				# If the existing zone is the same as the new zone (modulo the serial number),
				# there is no need to update the file. Unless we're forcing a bump.
				if zone == existing_zone and not force_bump and not force:
					return False

				# If the existing serial is not less than a serial number
				# based on the current date plus 00, increment it. Otherwise,
				# the serial number is less than our desired new serial number
				# so we'll use the desired new number.
				if existing_serial >= serial:
					serial = str(int(existing_serial) + 1)

	zone = zone.replace("__SERIAL__", serial)

	# Write the zone file.
	with open(zonefile, "w") as f:
		f.write(zone)

	return True # file is updated

########################################################################

def write_nsd_conf(zonefiles, env):
	# Basic header.
	nsdconf = """
server:
  hide-version: yes

  # identify the server (CH TXT ID.SERVER entry).
  identity: ""

  # The directory for zonefile: files.
  zonesdir: "/etc/nsd/zones"
"""
	
	# Since we have bind9 listening on localhost for locally-generated
	# DNS queries that require a recursive nameserver, and the system
	# might have other network interfaces for e.g. tunnelling, we have
	# to be specific about the network interfaces that nsd binds to.
	for ipaddr in (env.get("PRIVATE_IP", "") + " " + env.get("PRIVATE_IPV6", "")).split(" "):
		if ipaddr == "": continue
		nsdconf += "  ip-address: %s\n" % ipaddr

	# Append the zones.
	for domain, zonefile in zonefiles:
		nsdconf += """
zone:
	name: %s
	zonefile: %s
""" % (domain, zonefile)

	# Check if the nsd.conf is changing. If it isn't changing,
	# return False to flag that no change was made.
	with open("/etc/nsd/nsd.conf") as f:
		if f.read() == nsdconf:
			return False

	with open("/etc/nsd/nsd.conf", "w") as f:
		f.write(nsdconf)

	return True

########################################################################

def sign_zone(domain, zonefile, env):
	dnssec_keys = load_env_vars_from_file(os.path.join(env['STORAGE_ROOT'], 'dns/dnssec/keys.conf'))

	# In order to use the same keys for all domains, we have to generate
	# a new .key file with a DNSSEC record for the specific domain. We
	# can reuse the same key, but it won't validate without a DNSSEC
	# record specifically for the domain.
	# 
	# Copy the .key and .private files to /tmp to patch them up.
	#
	# Use os.umask and open().write() to securely create a copy that only
	# we (root) can read.
	files_to_kill = []
	for key in ("KSK", "ZSK"):
		if dnssec_keys.get(key, "").strip() == "": raise Exception("DNSSEC is not properly set up.")
		oldkeyfn = os.path.join(env['STORAGE_ROOT'], 'dns/dnssec/' + dnssec_keys[key])
		newkeyfn = '/tmp/' + dnssec_keys[key].replace("_domain_", domain)
		dnssec_keys[key] = newkeyfn
		for ext in (".private", ".key"):
			if not os.path.exists(oldkeyfn + ext): raise Exception("DNSSEC is not properly set up.")
			with open(oldkeyfn + ext, "r") as fr:
				keydata = fr.read()
			keydata = keydata.replace("_domain_", domain) # trick ldns-signkey into letting our generic key be used by this zone
			fn = newkeyfn + ext
			prev_umask = os.umask(0o77) # ensure written file is not world-readable
			try:
				with open(fn, "w") as fw:
					fw.write(keydata)
			finally:
				os.umask(prev_umask) # other files we write should be world-readable
			files_to_kill.append(fn)

	# Do the signing.
	expiry_date = (datetime.datetime.now() + datetime.timedelta(days=30)).strftime("%Y%m%d")
	shell('check_call', ["/usr/bin/ldns-signzone",
		# expire the zone after 30 days
		"-e", expiry_date,

		# use NSEC3
		"-n",

		# zonefile to sign
		"/etc/nsd/zones/" + zonefile,

		# keys to sign with (order doesn't matter -- it'll figure it out)
		dnssec_keys["KSK"],
		dnssec_keys["ZSK"],
	])

	# Create a DS record based on the patched-up key files. The DS record is specific to the
	# zone being signed, so we can't use the .ds files generated when we created the keys.
	# The DS record points to the KSK only. Write this next to the zone file so we can
	# get it later to give to the user with instructions on what to do with it.
	#
	# We want to be able to validate DS records too, but multiple forms may be valid depending
	# on the digest type. So we'll write all (both) valid records. Only one DS record should
	# actually be deployed. Preferebly the first.
	with open("/etc/nsd/zones/" + zonefile + ".ds", "w") as f:
		for digest_type in ('2', '1'):
			rr_ds = shell('check_output', ["/usr/bin/ldns-key2ds",
				"-n", # output to stdout
				"-" + digest_type, # 1=SHA1, 2=SHA256
				dnssec_keys["KSK"] + ".key"
			])
			f.write(rr_ds)

	# Remove our temporary file.
	for fn in files_to_kill:
		os.unlink(fn)

########################################################################

def write_opendkim_tables(zonefiles, env):
	# Append a record to OpenDKIM's KeyTable and SigningTable for each domain.

	opendkim_key_file = os.path.join(env['STORAGE_ROOT'], 'mail/dkim/mail.private')

	if not os.path.exists(opendkim_key_file):
		# Looks like OpenDKIM is not installed.
		return False

	config = {
		# The SigningTable maps email addresses to a key in the KeyTable that
		# specifies signing information for matching email addresses. Here we
		# map each domain to a same-named key.
		#
		# Elsewhere we set the DMARC policy for each domain such that mail claiming
		# to be From: the domain must be signed with a DKIM key on the same domain.
		# So we must have a separate KeyTable entry for each domain.
		"SigningTable":
			"".join(
				"*@{domain} {domain}\n".format(domain=domain)
				for domain, zonefile in zonefiles
			),

		# The KeyTable specifies the signing domain, the DKIM selector, and the
		# path to the private key to use for signing some mail. Per DMARC, the
		# signing domain must match the sender's From: domain.
		"KeyTable":
			"".join(
				"{domain} {domain}:mail:{key_file}\n".format(domain=domain, key_file=opendkim_key_file)
				for domain, zonefile in zonefiles
			),
	}

	did_update = False
	for filename, content in config.items():
		# Don't write the file if it doesn't need an update.
		if os.path.exists("/etc/opendkim/" + filename):
			with open("/etc/opendkim/" + filename) as f:
				if f.read() == content:
					continue

		# The contents needs to change.
		with open("/etc/opendkim/" + filename, "w") as f:
			f.write(content)
		did_update = True

	# Return whether the files changed. If they didn't change, there's
	# no need to kick the opendkim process.
	return did_update

########################################################################

def set_custom_dns_record(qname, rtype, value, env):
	# validate qname
	for zone, fn in get_dns_zones(env):
		# It must match a zone apex or be a subdomain of a zone
		# that we are otherwise hosting.
		if qname == zone or qname.endswith("."+zone):
			break
	else:
		# No match.
		raise ValueError("%s is not a domain name or a subdomain of a domain name managed by this box." % qname)

	# validate rtype
	rtype = rtype.upper()
	if value is not None:
		if rtype in ("A", "AAAA"):
			v = ipaddress.ip_address(value)
			if rtype == "A" and not isinstance(v, ipaddress.IPv4Address): raise ValueError("That's an IPv6 address.")
			if rtype == "AAAA" and not isinstance(v, ipaddress.IPv6Address): raise ValueError("That's an IPv4 address.")
		elif rtype in ("CNAME", "TXT"):
			# anything goes
			pass
		else:
			raise ValueError("Unknown record type '%s'." % rtype)

	# load existing config
	config = get_custom_dns_config(env)

	# update
	if qname not in config:
		if value is None:
			# Is asking to delete a record that does not exist.
			return False
		elif rtype == "A":
			# Add this record using the short form 'qname: value'.
			config[qname] = value
		else:
			# Add this record. This is the qname's first record.
			config[qname] = { rtype: value }
	else:
		if isinstance(config[qname], str):
			# This is a short-form 'qname: value' implicit-A record.
			if value is None and rtype != "A":
				# Is asking to delete a record that doesn't exist.
				return False
			elif value is None and rtype == "A":
				# Delete record.
				del config[qname]
			elif rtype == "A":
				# Update, keeping short form.
				if config[qname] == "value":
					# No change.
					return False
				config[qname] = value
			else:
				# Expand short form so we can add a new record type.
				config[qname] = { "A": config[qname], rtype: value }
		else:
			# This is the qname: { ... } (dict) format.
			if value is None:
				if rtype not in config[qname]:
					# Is asking to delete a record that doesn't exist.
					return False
				else:
					# Delete the record. If it's the last record, delete the domain.
					del config[qname][rtype]
					if len(config[qname]) == 0:
						del config[qname]
			else:
				# Update the record.
				if config[qname].get(rtype) == "value":
					# No change.
					return False
				config[qname][rtype] = value

	# serialize & save
	config_yaml = rtyaml.dump(config)
	with open(os.path.join(env['STORAGE_ROOT'], 'dns/custom.yaml'), "w") as f:
		f.write(config_yaml)

	return True

########################################################################

def justtestingdotemail(domain, records):
	# If the domain is a subdomain of justtesting.email, which we own,
	# automatically populate the zone where it is set up on dns4e.com.
	# Ideally if dns4e.com supported NS records we would just have it
	# delegate DNS to us, but instead we will populate the whole zone.

	import subprocess, json, urllib.parse

	if not domain.endswith(".justtesting.email"):
		return

	for subdomain, querytype, value, explanation in records:
		if querytype in ("NS",): continue
		if subdomain in ("www", "ns1", "ns2"): continue # don't do unnecessary things

		if subdomain == None:
			subdomain = domain
		else:
			subdomain = subdomain + "." + domain

		if querytype == "TXT":
			# nsd requires parentheses around txt records with multiple parts,
			# but DNS4E requires there be no parentheses; also it goes into
			# nsd with a newline and a tab, which we replace with a space here
			value = re.sub("^\s*\(\s*([\w\W]*)\)", r"\1", value)
			value = re.sub("\s+", " ", value)
		else:
			continue

		print("Updating DNS for %s/%s..." % (subdomain, querytype))
		resp = json.loads(subprocess.check_output([
			"curl",
			"-s",
			"https://api.dns4e.com/v7/%s/%s" % (urllib.parse.quote(subdomain), querytype.lower()),
			"--user", "2ddbd8e88ed1495fa0ec:A97TDJV26CVUJS6hqAs0CKnhj4HvjTM7MwAAg8xb",
			"--data", "record=%s" % urllib.parse.quote(value),
			]).decode("utf8"))
		print("\t...", resp.get("message", "?"))

########################################################################

def build_recommended_dns(env):
	ret = []
	domains = get_dns_domains(env)
	zonefiles = get_dns_zones(env)
	additional_records = get_custom_dns_config(env)
	for domain, zonefile in zonefiles:
		records = build_zone(domain, domains, additional_records, env)

		# remove records that we don't dislay
		records = [r for r in records if r[3] is not False]

		# put Required at the top, then Recommended, then everythiing else
		records.sort(key = lambda r : 0 if r[3].startswith("Required.") else (1 if r[3].startswith("Recommended.") else 2))

		# expand qnames
		for i in range(len(records)):
			if records[i][0] == None:
				qname = domain
			else:
				qname = records[i][0] + "." + domain

			records[i] = {
				"qname": qname,
				"rtype": records[i][1],
				"value": records[i][2],
				"explanation": records[i][3],
			}

		# return
		ret.append((domain, records))
	return ret

if __name__ == "__main__":
	from utils import load_environment
	env = load_environment()
	for zone, records in build_recommended_dns(env):
		for record in records:
			print("; " + record['explanation'])
			print(record['qname'], record['rtype'], record['value'], sep="\t")
			print()
