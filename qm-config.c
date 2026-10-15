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

typedef struct {
	/* json_type_null delegates type checking to parse (e.g. size strings/ints).
	 * For arrays, parse handles one element and appends to the target PtrArray.
	 */
	enum json_type type;
	void (*parse)(void *field, json_object *value, const char *name,
		      const char *filename);
	void (*destroy)(void *value); /* Owned pointer, or an array element. */
} ConfigParser;

typedef struct {
	const char *name;
	size_t offset;
	const ConfigParser *parser;
	bool required;
	bool track_presence;
	size_t presence_offset;
} ConfigEntry;

#define CONFIG_ENTRY(struct_type, key, member, value_parser, ...)                        \
	{.name = key,                                                                    \
	 .offset = offsetof(struct_type, member),                                        \
	 .parser = &value_parser,                                                        \
	 __VA_ARGS__}

static void parse_object(void *target, json_object *object, const ConfigEntry *entries,
			 size_t count, const char *name, const char *filename)
{
	if (!json_object_is_type(object, json_type_object))
		errx(1, "%s: %s must be a JSON object", filename, name);

	json_object_object_foreach (object, key, unused) {
		(void)unused;
		size_t i;
		for (i = 0; i < count; i++)
			if (!strcmp(key, entries[i].name))
				break;
		if (i == count)
			errx(1, "%s: unknown %s key: %s", filename, name, key);
	}

	/* Table order also defines precedence between fields that share a list. */
	for (size_t i = 0; i < count; i++) {
		const ConfigEntry *entry = &entries[i];
		json_object *value;
		if (!json_object_object_get_ex(object, entry->name, &value)) {
			if (entry->required)
				errx(1, "%s: %s.%s is required", filename, name,
				     entry->name);
			continue;
		}

		autofree char *field_name = xasprintf("%s.%s", name, entry->name);
		const ConfigParser *parser = entry->parser;
		if (parser->type != json_type_null &&
		    !json_object_is_type(value, parser->type))
			errx(1, "%s: %s must be a JSON %s", filename, field_name,
			     json_type_to_name(parser->type));

		void *field = (char *)target + entry->offset;
		if (parser->type == json_type_array) {
			for (size_t j = 0; j < json_object_array_length(value); j++) {
				autofree char *item_name =
					xasprintf("%s[%zu]", field_name, j);
				parser->parse(field, json_object_array_get_idx(value, j),
					      item_name, filename);
			}
		} else {
			parser->parse(field, value, field_name, filename);
		}

		if (entry->track_presence)
			*(bool *)((char *)target + entry->presence_offset) = true;
	}
}

static void clear_object(void *target, const ConfigEntry *entries, size_t count)
{
	for (size_t i = 0; i < count; i++) {
		const ConfigEntry *entry = &entries[i];
		if (!entry->parser->destroy)
			continue;

		void *field = (char *)target + entry->offset;
		if (entry->parser->type == json_type_array) {
			ptr_array_clear(field, entry->parser->destroy);
		} else {
			entry->parser->destroy(*(void **)field);
			*(void **)field = NULL;
		}
	}
}

static const char *config_string(json_object *value, const char *name,
				 const char *filename)
{
	if (!json_object_is_type(value, json_type_string))
		errx(1, "%s: %s must be a JSON string", filename, name);

	const char *string = json_object_get_string(value);
	if (strlen(string) != (size_t)json_object_get_string_len(value))
		errx(1, "%s: %s contains a NUL byte", filename, name);

	return string;
}

static void parse_memory(void *field, json_object *value, const char *name,
			 const char *filename)
{
	if (json_object_is_type(value, json_type_int)) {
		if (json_object_get_int64(value) <= 0 ||
		    json_object_get_uint64(value) > INT64_MAX)
			errx(1, "%s: memory-max must be positive or infinity", filename);
		*(int64_t *)field = json_object_get_int64(value);
		return;
	}

	const char *string = config_string(value, name, filename);
	if (!parse_size(string, field))
		errx(1, "%s: invalid memory-max: %s", filename, string);
}

static char *absolute_path(json_object *value, const char *name, const char *filename)
{
	const char *path = config_string(value, name, filename);
	if (*path != '/')
		errx(1, "%s: %s must be an absolute path", filename, name);

	return canonicalize_path(path);
}

static void replace_string(void *field, char *string)
{
	free(*(char **)field);
	*(char **)field = string;
}

static void parse_path(void *field, json_object *value, const char *name,
		       const char *filename)
{
	replace_string(field, absolute_path(value, name, filename));
}

static void parse_hostname(void *field, json_object *value, const char *name,
			   const char *filename)
{
	const char *string = config_string(value, name, filename);
	if (!*string || strlen(string) > 64 ||
	    strspn(string, "abcdefghijklmnopqrstuvwxyz"
			   "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
			   "0123456789.-") != strlen(string))
		errx(1, "%s: invalid hostname", filename);

	replace_string(field, xstrdup(string));
}

static void parse_ipv4(void *field, json_object *value, const char *name,
		       const char *filename)
{
	const char *string = config_string(value, name, filename);
	struct in_addr address;
	if (inet_pton(AF_INET, string, &address) != 1)
		errx(1, "%s: %s must be an IPv4 address", filename, name);

	uint32_t ip = ntohl(address.s_addr);
	if (!(ip >> 24) || (ip >> 24) == 127 || (ip >> 24) >= 224)
		errx(1, "%s: %s must be a unicast IPv4 address", filename, name);

	replace_string(field, xstrdup(string));
}

static void parse_prefix_length(void *field, json_object *value, const char *name,
				const char *filename)
{
	int64_t prefix = json_object_get_int64(value);
	if (prefix < 1 || prefix > 31)
		errx(1, "%s: %s must be between 1 and 31", filename, name);

	*(unsigned int *)field = prefix;
}

static void parse_userns(void *field, json_object *value, const char *name,
			 const char *filename)
{
	static const char *const names[] = {
		[USERNS_AUTO] = "auto",
		[USERNS_OFF] = "off",
		[USERNS_REQUIRED] = "required",
	};
	const char *string = config_string(value, name, filename);
	for (size_t i = 0; i < N_ELEMENTS(names); i++) {
		if (!strcmp(string, names[i])) {
			*(UserNamespaceMode *)field = i;
			return;
		}
	}

	errx(1, "%s: invalid userns mode: %s", filename, string);
}

static void parse_bool(void *field, json_object *value, const char *name,
		       const char *filename)
{
	(void)name;
	(void)filename;
	*(bool *)field = json_object_get_boolean(value);
}

static void append_string(void *field, json_object *value, const char *name,
			  const char *filename)
{
	ptr_array_add(field, xstrdup(config_string(value, name, filename)));
}

static const ConfigParser path_parser = {json_type_string, parse_path, free};
static const ConfigParser hostname_parser = {json_type_string, parse_hostname, free};
static const ConfigParser ipv4_parser = {json_type_string, parse_ipv4, free};
static const ConfigParser prefix_parser = {json_type_int, parse_prefix_length, NULL};
static const ConfigParser userns_parser = {json_type_string, parse_userns, NULL};
static const ConfigParser bool_parser = {json_type_boolean, parse_bool, NULL};
static const ConfigParser memory_parser = {json_type_null, parse_memory, NULL};
static const ConfigParser strings_parser = {json_type_array, append_string, free};

static const ConfigEntry mount_entries[] = {
	CONFIG_ENTRY(PathOperation, "source", source, path_parser, .required = true),
	CONFIG_ENTRY(PathOperation, "destination", destination, path_parser,
		     .required = true),
	CONFIG_ENTRY(PathOperation, "remove", remove, bool_parser),
	CONFIG_ENTRY(PathOperation, "optional", optional, bool_parser),
	CONFIG_ENTRY(PathOperation, "options", options, strings_parser,
		     .track_presence = true,
		     .presence_offset = offsetof(PathOperation, options_set)),
};

static const ConfigEntry device_entries[] = {
	CONFIG_ENTRY(PathOperation, "source", source, path_parser, .required = true),
	CONFIG_ENTRY(PathOperation, "destination", destination, path_parser),
	CONFIG_ENTRY(PathOperation, "remove", remove, bool_parser),
	CONFIG_ENTRY(PathOperation, "optional", optional, bool_parser),
};

static const ConfigEntry removal_entries[] = {
	CONFIG_ENTRY(PathOperation, "destination", destination, path_parser,
		     .required = true),
	CONFIG_ENTRY(PathOperation, "remove", remove, bool_parser),
};

static void path_operation_free(void *data)
{
	clear_object(data, mount_entries, N_ELEMENTS(mount_entries));
	free(data);
}

static bool reserved_mount(const char *destination)
{
	const char *const reserved[] = {"/",
					"/run/.containerenv",
					"/run/secrets",
					"/etc/hostname",
					"/etc/resolv.conf",
					"/etc/hosts"};

	for (size_t i = 0; i < N_ELEMENTS(reserved); i++)
		if (!strcmp(destination, reserved[i]))
			return true;

	return false;
}

static void parse_path_operation(PtrArray *operations, json_object *value, bool device,
				 const char *name, const char *filename)
{
	bool remove = json_object_is_type(value, json_type_object) &&
		      json_object_get_boolean(js_get(value, "remove"));
	const ConfigEntry *entries = mount_entries;
	size_t count = N_ELEMENTS(mount_entries);
	if (remove) {
		entries = removal_entries;
		count = N_ELEMENTS(removal_entries);
	} else if (device) {
		entries = device_entries;
		count = N_ELEMENTS(device_entries);
	}

	PathOperation *operation = xnew(PathOperation);
	*operation = (PathOperation){.device = device};
	ptr_array_add(operations, operation);
	parse_object(operation, value, entries, count, name, filename);

	if (!operation->destination)
		operation->destination = xstrdup(operation->source);
	if (reserved_mount(operation->destination))
		errx(1, "%s: reserved mount destination: %s", filename,
		     operation->destination);
}

static void parse_mount(void *field, json_object *value, const char *name,
			const char *filename)
{
	parse_path_operation(field, value, false, name, filename);
}

static void parse_device(void *field, json_object *value, const char *name,
			 const char *filename)
{
	parse_path_operation(field, value, true, name, filename);
}

static const char *const capability_names[] = {
	"CAP_CHOWN",
	"CAP_DAC_OVERRIDE",
	"CAP_DAC_READ_SEARCH",
	"CAP_FOWNER",
	"CAP_FSETID",
	"CAP_KILL",
	"CAP_SETGID",
	"CAP_SETUID",
	"CAP_SETPCAP",
	"CAP_LINUX_IMMUTABLE",
	"CAP_NET_BIND_SERVICE",
	"CAP_NET_BROADCAST",
	"CAP_NET_ADMIN",
	"CAP_NET_RAW",
	"CAP_IPC_LOCK",
	"CAP_IPC_OWNER",
	"CAP_SYS_MODULE",
	"CAP_SYS_RAWIO",
	"CAP_SYS_CHROOT",
	"CAP_SYS_PTRACE",
	"CAP_SYS_PACCT",
	"CAP_SYS_ADMIN",
	"CAP_SYS_BOOT",
	"CAP_SYS_NICE",
	"CAP_SYS_RESOURCE",
	"CAP_SYS_TIME",
	"CAP_SYS_TTY_CONFIG",
	"CAP_MKNOD",
	"CAP_LEASE",
	"CAP_AUDIT_WRITE",
	"CAP_AUDIT_CONTROL",
	"CAP_SETFCAP",
	"CAP_MAC_OVERRIDE",
	"CAP_MAC_ADMIN",
	"CAP_SYSLOG",
	"CAP_WAKE_ALARM",
	"CAP_BLOCK_SUSPEND",
	"CAP_AUDIT_READ",
	"CAP_PERFMON",
	"CAP_BPF",
	"CAP_CHECKPOINT_RESTORE",
};

static void parse_capability(PtrArray *operations, json_object *value, bool remove,
			     const char *field_name, const char *filename)
{
	const char *name = config_string(value, field_name, filename);
	bool all = !strcasecmp(name, "all"), found = false;
	if (!strncasecmp(name, "CAP_", 4))
		name += 4;

	for (size_t i = 0; i < N_ELEMENTS(capability_names); i++) {
		if (!all && strcasecmp(name, capability_names[i] + 4))
			continue;

		found = true;
		CapabilityOperation *operation = xnew(CapabilityOperation);
		*operation = (CapabilityOperation){
			.name = capability_names[i],
			.remove = remove,
		};
		ptr_array_add(operations, operation);
	}

	if (!found)
		errx(1, "%s: unknown capability: %s", filename, name);
}

static void parse_add_capability(void *field, json_object *value, const char *name,
				 const char *filename)
{
	parse_capability(field, value, false, name, filename);
}

static void parse_drop_capability(void *field, json_object *value, const char *name,
				  const char *filename)
{
	parse_capability(field, value, true, name, filename);
}

static void parse_unmask(void *field, json_object *value, const char *name,
			 const char *filename)
{
	const char *path = config_string(value, name, filename);
	char *canonical;
	if (strcmp(path, "ALL"))
		canonical = absolute_path(value, name, filename);
	else
		canonical = xstrdup(path);

	ptr_array_add(field, canonical);
}

static const ConfigParser mounts_parser = {json_type_array, parse_mount,
					   path_operation_free};
static const ConfigParser devices_parser = {json_type_array, parse_device,
					    path_operation_free};
static const ConfigParser add_caps_parser = {json_type_array, parse_add_capability, free};
static const ConfigParser drop_caps_parser = {json_type_array, parse_drop_capability,
					      free};
static const ConfigParser unmask_parser = {json_type_array, parse_unmask, free};

static const ConfigEntry config_entries[] = {
	CONFIG_ENTRY(Config, "base-config", base_config, path_parser),
	CONFIG_ENTRY(Config, "hostname", hostname, hostname_parser),
	CONFIG_ENTRY(Config, "rootfs", rootfs, path_parser),
	CONFIG_ENTRY(Config, "etc-dir", etc_dir, path_parser),
	CONFIG_ENTRY(Config, "var-dir", var_dir, path_parser),
	CONFIG_ENTRY(Config, "seccomp", seccomp_file, path_parser),
	CONFIG_ENTRY(Config, "resolv-conf", resolv_conf, path_parser),
	CONFIG_ENTRY(Config, "ip", ip, ipv4_parser),
	CONFIG_ENTRY(Config, "gateway", gateway, ipv4_parser),
	CONFIG_ENTRY(Config, "prefix-length", prefix_length, prefix_parser),
	CONFIG_ENTRY(Config, "userns", userns, userns_parser),
	CONFIG_ENTRY(Config, "verbose", verbose, bool_parser),
	CONFIG_ENTRY(Config, "memory-max", memory_max, memory_parser,
		     .track_presence = true,
		     .presence_offset = offsetof(Config, memory_max_set)),

	/* These pairs share a target list; table order defines per-file precedence. */
	CONFIG_ENTRY(Config, "mounts", path_operations, mounts_parser),
	CONFIG_ENTRY(Config, "devices", path_operations, devices_parser),
	CONFIG_ENTRY(Config, "drop-capabilities", capability_operations,
		     drop_caps_parser),
	CONFIG_ENTRY(Config, "add-capabilities", capability_operations, add_caps_parser),
	CONFIG_ENTRY(Config, "unmask", unmask, unmask_parser),
};

void config_free(Config *config)
{
	if (!config)
		return;

	clear_object(config, config_entries, N_ELEMENTS(config_entries));
	free(config);
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

	parse_object(config, layer, config_entries, N_ELEMENTS(config_entries),
		     "configuration", filename);
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
