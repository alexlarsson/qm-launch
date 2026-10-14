/* SPDX-License-Identifier: LGPL-2.1-or-later
 * Copyright (C) 2026 Alexander Larsson
 * See COPYING for the full license text.
 */

#define _GNU_SOURCE
#include "qm-config.h"
#include "config.h"
#include "qm-utils.h"

#include <arpa/inet.h>
#include <dirent.h>
#include <err.h>
#include <errno.h>
#include <limits.h>
#include <stddef.h>
#include <string.h>

#define DEFAULT_HOSTNAME "qm"

typedef enum {
	CONFIG_STRING,
	CONFIG_PATH,
	CONFIG_HOSTNAME,
	CONFIG_IPV4,
	CONFIG_PREFIX_LENGTH,
	CONFIG_USERNS,
	CONFIG_BOOL,
} ConfigValueType;

typedef struct {
	const char *name;
	size_t offset;
	ConfigValueType type;
} ConfigEntry;

#define CONFIG_ENTRY(name, member, type) {name, offsetof(Config, member), type}

static const ConfigEntry config_entries[] = {
	CONFIG_ENTRY("base-config", base_config, CONFIG_PATH),
	CONFIG_ENTRY("hostname", hostname, CONFIG_HOSTNAME),
	CONFIG_ENTRY("rootfs", rootfs, CONFIG_PATH),
	CONFIG_ENTRY("etc-dir", etc_dir, CONFIG_PATH),
	CONFIG_ENTRY("var-dir", var_dir, CONFIG_PATH),
	CONFIG_ENTRY("seccomp", seccomp_file, CONFIG_PATH),
	CONFIG_ENTRY("resolv-conf", resolv_conf, CONFIG_PATH),
	CONFIG_ENTRY("ip", ip, CONFIG_IPV4),
	CONFIG_ENTRY("gateway", gateway, CONFIG_IPV4),
	CONFIG_ENTRY("prefix-length", prefix_length, CONFIG_PREFIX_LENGTH),
	CONFIG_ENTRY("userns", userns, CONFIG_USERNS),
	CONFIG_ENTRY("verbose", verbose, CONFIG_BOOL),
};

static const char *const userns_names[] = {
	[USERNS_AUTO] = "auto",
	[USERNS_OFF] = "off",
	[USERNS_REQUIRED] = "required",
};

static bool string_type(ConfigValueType type)
{
	return type == CONFIG_STRING || type == CONFIG_PATH || type == CONFIG_HOSTNAME ||
	       type == CONFIG_IPV4;
}

void config_free(Config *config)
{
	if (!config)
		return;
	for (size_t i = 0; i < N_ELEMENTS(config_entries); i++) {
		const ConfigEntry *entry = &config_entries[i];
		if (string_type(entry->type))
			free(*(char **)((char *)config + entry->offset));
	}
	free(config);
}

static void parse_entry(Config *config, const ConfigEntry *entry, json_object *value,
			const char *filename)
{
	bool boolean = entry->type == CONFIG_BOOL;
	bool integer = entry->type == CONFIG_PREFIX_LENGTH;
	enum json_type expected_type = json_type_string;
	if (boolean)
		expected_type = json_type_boolean;
	else if (integer)
		expected_type = json_type_int;
	if (!json_object_is_type(value, expected_type))
		errx(1, "%s: %s must be a JSON %s", filename, entry->name,
		     json_type_to_name(expected_type));
	void *field = (char *)config + entry->offset;
	if (boolean) {
		*(bool *)field = json_object_get_boolean(value);
		return;
	}
	if (integer) {
		int64_t prefix = json_object_get_int64(value);
		if (prefix < 1 || prefix > 31)
			errx(1, "%s: %s must be between 1 and 31", filename, entry->name);
		*(unsigned int *)field = prefix;
		return;
	}

	const char *string = json_object_get_string(value);
	if (strlen(string) != (size_t)json_object_get_string_len(value))
		errx(1, "%s: %s contains a NUL byte", filename, entry->name);
	switch (entry->type) {
	case CONFIG_IPV4: {
		struct in_addr address;
		if (inet_pton(AF_INET, string, &address) != 1)
			errx(1, "%s: %s must be an IPv4 address", filename, entry->name);
		uint32_t ip = ntohl(address.s_addr);
		if (!(ip >> 24) || (ip >> 24) == 127 || (ip >> 24) >= 224)
			errx(1, "%s: %s must be a unicast IPv4 address", filename,
			     entry->name);
		break;
	}
	case CONFIG_PATH:
		if (string[0] != '/')
			errx(1, "%s: %s must be an absolute path: %s", filename,
			     entry->name, string);
		break;
	case CONFIG_HOSTNAME:
		if (!*string || strlen(string) > 64 ||
		    strspn(string, "abcdefghijklmnopqrstuvwxyz"
				   "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
				   "0123456789.-") != strlen(string))
			errx(1, "%s: invalid hostname", filename);
		break;
	case CONFIG_USERNS:
		for (size_t i = 0; i < N_ELEMENTS(userns_names); i++) {
			if (!strcmp(string, userns_names[i])) {
				*(UserNamespaceMode *)field = i;
				return;
			}
		}
		errx(1, "%s: invalid userns mode: %s", filename, string);
	case CONFIG_STRING:
		break;
	case CONFIG_BOOL:
	case CONFIG_PREFIX_LENGTH:
		/* Handled above. */
		abort();
	}

	free(*(char **)field);
	*(char **)field = xstrdup(string);
}

static void validate_network(Config *config)
{
	struct in_addr address, gateway;
	if (inet_pton(AF_INET, config->ip, &address) != 1 ||
	    inet_pton(AF_INET, config->gateway, &gateway) != 1)
		abort(); /* Individual values are validated while parsing. */
	uint32_t ip = ntohl(address.s_addr), gw = ntohl(gateway.s_addr);
	uint32_t mask = UINT32_MAX << (32 - config->prefix_length);
	if (ip == gw || (ip & mask) != (gw & mask))
		errx(1, "ip and gateway must be distinct addresses in the same subnet");
	/* /31 has two usable endpoints. Shorter prefixes reserve both ends. */
	if (config->prefix_length < 31 && (!(ip & ~mask) || (ip & ~mask) == ~mask ||
					   !(gw & ~mask) || (gw & ~mask) == ~mask))
		errx(1, "ip and gateway must not be network or broadcast addresses");
	struct in_addr subnet = {.s_addr = htonl(ip & mask)};
	if (!inet_ntop(AF_INET, &subnet, config->subnet, sizeof(config->subnet)))
		err(1, "format network subnet");
}

static void merge_file(Config *config, const char *filename, bool optional)
{
	autofree char *contents = try_read_text_file(filename);
	if (!contents) {
		if (optional && errno == ENOENT)
			return;
		err(1, "read %s", filename);
	}
	if (strlen(contents) >= INT_MAX)
		errx(1, "%s: configuration too large", filename);

	json_tokener *tokener = json_tokener_new();
	if (!tokener)
		oom();
	json_tokener_set_flags(tokener, JSON_TOKENER_STRICT);

	autofreejson json_object *layer =
		json_tokener_parse_ex(tokener, contents, strlen(contents) + 1);
	enum json_tokener_error error = json_tokener_get_error(tokener);
	json_tokener_free(tokener);
	if (error != json_tokener_success)
		errx(1, "%s: %s", filename, json_tokener_error_desc(error));
	if (!json_object_is_type(layer, json_type_object))
		errx(1, "%s: expected a JSON object", filename);

	json_object_object_foreach (layer, key, value) {
		const ConfigEntry *entry = NULL;
		for (size_t i = 0; i < N_ELEMENTS(config_entries); i++) {
			if (!strcmp(key, config_entries[i].name)) {
				entry = &config_entries[i];
				break;
			}
		}
		if (!entry)
			errx(1, "%s: unknown configuration key: %s", filename, key);
		parse_entry(config, entry, value, filename);
	}
}

static int conf_filter(const struct dirent *entry)
{
	size_t length = strlen(entry->d_name);
	return length >= 5 && !strcmp(entry->d_name + length - 5, ".conf");
}

/* Byte ordering is deterministic even when the host locale changes. */
static int conf_compare(const struct dirent **a, const struct dirent **b)
{
	return strcmp((*a)->d_name, (*b)->d_name);
}

static void merge_directory(Config *config, const char *directory)
{
	autofree char *main_file = join_path(directory, "qmc.conf");
	merge_file(config, main_file, true);
	autofree char *dropins = join_path(directory, "qmc.conf.d");
	struct dirent **entries;
	int count = scandir(dropins, &entries, conf_filter, conf_compare);
	if (count < 0) {
		if (errno == ENOENT)
			return;
		err(1, "scan %s", dropins);
	}
	for (int i = 0; i < count; i++) {
		autofree char *path = join_path(dropins, entries[i]->d_name);
		merge_file(config, path, false);
		free(entries[i]);
	}
	free(entries);
}

Config *configuration_load(const char *vendor_dir, const char *admin_dir,
			   const char *filename)
{
	autofreeconfig Config *config = xnew(Config);
	*config = (Config){
		.base_config = xstrdup(QM_DATA_DIR "/qm.json"),
		.hostname = xstrdup(DEFAULT_HOSTNAME),
		.ip = xstrdup("10.90.0.2"),
		.gateway = xstrdup("10.90.0.1"),
		.prefix_length = 24,
		.resolv_conf =
			xstrdup(access("/run/systemd/resolve/resolv.conf", R_OK) == 0
					? "/run/systemd/resolve/resolv.conf"
					: "/etc/resolv.conf"),
		.userns = USERNS_AUTO,
	};
	if (filename)
		merge_file(config, filename, false);
	else {
		merge_directory(config, vendor_dir);
		merge_directory(config, admin_dir);
	}
	validate_network(config);
	return steal_ptr(&config);
}
