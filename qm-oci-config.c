/* SPDX-License-Identifier: LGPL-2.1-or-later
 * Copyright (C) 2026 Alexander Larsson
 * See COPYING for the full license text.
 */

#include "qm-oci-config.h"
#include "qm-utils.h"

#include <err.h>
#include <errno.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>

static json_object *ensure_object(json_object *parent, const char *name)
{
	if (!js_get(parent, name))
		js_object_add(parent, name, js_new_object());

	return js_need(parent, name, json_type_object);
}

static json_object *ensure_array(json_object *parent, const char *name)
{
	if (!js_get(parent, name))
		js_object_add(parent, name, js_new_array());

	return js_need(parent, name, json_type_array);
}

static void remove_path(json_object *array, const char *key, const char *path)
{
	if (!array)
		return;

	for (size_t i = json_object_array_length(array); i > 0; i--) {
		json_object *item = json_object_array_get_idx(array, i - 1);
		if (!strcmp(js_string(item, key), path))
			json_object_array_del_idx(array, i - 1, 1);
	}
}

/* Only remove an exact allow rule when no remaining OCI device needs it.
 * Broad rules and deny rules belong to the base policy and are retained.
 */
static void remove_device_rules(json_object *linux_config, json_object *removed)
{
	json_object *devices = js_get(linux_config, "devices");
	int64_t device_major = json_object_get_int64(js_get(removed, "major"));
	int64_t device_minor = json_object_get_int64(js_get(removed, "minor"));
	const char *type = js_string(removed, "type");

	for (size_t i = 0; devices && i < json_object_array_length(devices); i++) {
		json_object *other = json_object_array_get_idx(devices, i);
		if (!strcmp(js_string(other, "type"), type) &&
		    json_object_get_int64(js_get(other, "major")) == device_major &&
		    json_object_get_int64(js_get(other, "minor")) == device_minor)
			return;
	}

	json_object *rules = js_get(js_get(linux_config, "resources"), "devices");
	for (size_t i = rules ? json_object_array_length(rules) : 0; i > 0; i--) {
		json_object *rule = json_object_array_get_idx(rules, i - 1);
		json_object *rule_type = js_get(rule, "type");

		if (json_object_get_boolean(js_get(rule, "allow")) && rule_type &&
		    !strcmp(json_object_get_string(rule_type), type) &&
		    js_get(rule, "major") && js_get(rule, "minor") &&
		    json_object_get_int64(js_get(rule, "major")) == device_major &&
		    json_object_get_int64(js_get(rule, "minor")) == device_minor)
			json_object_array_del_idx(rules, i - 1, 1);
	}
}

static void remove_device(json_object *linux_config, const char *destination)
{
	json_object *devices = js_get(linux_config, "devices");
	for (size_t i = devices ? json_object_array_length(devices) : 0; i > 0; i--) {
		json_object *device = json_object_array_get_idx(devices, i - 1);
		if (strcmp(js_string(device, "path"), destination))
			continue;

		autofreejson json_object *removed = json_object_get(device);
		json_object_array_del_idx(devices, i - 1, 1);
		remove_device_rules(linux_config, removed);
	}
}

static void add_device_rule(json_object *linux_config, const struct stat *st)
{
	json_object *rules =
		ensure_array(ensure_object(linux_config, "resources"), "devices");

	autofreejson json_object *rule = js_new_object();
	js_object_add(rule, "allow", js_new_bool(true));
	js_set_string(rule, "type", S_ISCHR(st->st_mode) ? "c" : "b");
	js_object_add(rule, "major", js_new_int(major(st->st_rdev)));
	js_object_add(rule, "minor", js_new_int(minor(st->st_rdev)));
	js_set_string(rule, "access", "rwm");

	js_array_add(rules, steal_ptr(&rule));
}

static void configure_paths(json_object *oci, const Config *config)
{
	if (!config->path_operations.length)
		return;

	/* Last operation per destination wins. Resolve before probing sources or
	 * granting device access, so removed/replaced devices leave no rules.
	 */
	PtrArray resolved = {0};
	for (size_t i = 0; i < config->path_operations.length; i++) {
		const PathOperation *operation = config->path_operations.items[i];
		size_t j;
		for (j = 0; j < resolved.length; j++) {
			const PathOperation *previous = resolved.items[j];
			if (!strcmp(previous->destination, operation->destination))
				break;
		}

		if (j < resolved.length)
			resolved.items[j] = config->path_operations.items[i];
		else
			ptr_array_add(&resolved, config->path_operations.items[i]);
	}

	json_object *mounts = js_need(oci, "mounts", json_type_array);
	json_object *linux_config = js_need(oci, "linux", json_type_object);

	/* Remove base device nodes/rules before adding any new access rules. */
	for (size_t i = 0; i < resolved.length; i++) {
		const PathOperation *operation = resolved.items[i];
		remove_device(linux_config, operation->destination);
	}

	for (size_t i = 0; i < resolved.length; i++) {
		const PathOperation *operation = resolved.items[i];
		const char *destination = operation->destination;
		const char *source = operation->source;
		bool remove = operation->remove;
		bool device = operation->device;
		struct stat st;

		if (!remove && (device || operation->optional)) {
			if (stat(source, &st) < 0) {
				if (operation->optional && errno == ENOENT)
					remove = true;
				else
					err(1, "stat %s", source);
			}

			if (!remove && device && !S_ISCHR(st.st_mode) &&
			    !S_ISBLK(st.st_mode))
				errx(1, "%s is not a character or block device", source);
		}

		if (remove) {
			remove_path(mounts, "destination", destination);
			continue;
		}

		autofreejson json_object *mount = js_new_object();
		js_set_string(mount, "destination", destination);
		js_set_string(mount, "source", source);
		js_set_string(mount, "type", "bind");

		if (operation->options_set) {
			autofreejson json_object *options = js_new_array();
			for (size_t j = 0; j < operation->options.length; j++)
				js_array_add(options,
					     js_new_string(operation->options.items[j]));
			js_object_add(mount, "options", steal_ptr(&options));
		} else if (device) {
			const char *const flags[] = {"bind", "rprivate", "nosuid",
						     "noexec"};
			js_object_add(mount, "options",
				      js_new_strings(flags, N_ELEMENTS(flags)));
		} else {
			const char *const flags[] = {"rbind", "rprivate"};
			js_object_add(mount, "options",
				      js_new_strings(flags, N_ELEMENTS(flags)));
		}

		/* Replace in place so parent mounts stay before their children. */
		bool replaced = false;
		for (size_t j = 0; j < json_object_array_length(mounts);) {
			json_object *old = json_object_array_get_idx(mounts, j);
			if (strcmp(js_string(old, "destination"), destination)) {
				j++;
			} else if (!replaced) {
				json_object_array_put_idx(mounts, j++,
							  json_object_get(mount));
				replaced = true;
			} else {
				json_object_array_del_idx(mounts, j, 1);
			}
		}

		if (!replaced)
			js_array_add(mounts, json_object_get(mount));

		if (device)
			add_device_rule(linux_config, &st);
	}

	ptr_array_clear(&resolved, NULL);
}

static void configure_capabilities(json_object *oci, const Config *config)
{
	if (!config->capability_operations.length)
		return;

	json_object *caps =
		ensure_object(js_need(oci, "process", json_type_object), "capabilities");
	const char *const sets[] = {"bounding", "effective", "permitted", "inheritable",
				    "ambient"};

	for (size_t i = 0; i < config->capability_operations.length; i++) {
		const CapabilityOperation *operation =
			config->capability_operations.items[i];
		const char *name = operation->name;
		bool remove = operation->remove;

		for (size_t j = 0; j < N_ELEMENTS(sets); j++) {
			/* Add to the three normal sets; remove from all five. */
			json_object *set = js_get(caps, sets[j]);
			if (!remove && j < 3)
				set = ensure_array(caps, sets[j]);
			if (!set)
				continue;

			if (remove) {
				for (size_t k = json_object_array_length(set); k > 0; k--)
					if (!strcmp(js_array_string(set, k - 1), name))
						json_object_array_del_idx(set, k - 1, 1);
			} else if (j < 3 && !js_contains(set, name)) {
				js_array_add(set, js_new_string(name));
			}
		}
	}
}

void configure_oci_policy(json_object *oci, const Config *config)
{
	json_object *linux_config = js_need(oci, "linux", json_type_object);
	if (config->memory_max_set) {
		json_object *memory =
			ensure_object(ensure_object(linux_config, "resources"), "memory");
		js_object_add(memory, "limit", js_new_int(config->memory_max));
	}

	configure_capabilities(oci, config);

	const char *const path_lists[] = {"maskedPaths", "readonlyPaths"};
	for (size_t i = 0; i < config->unmask.length; i++) {
		const char *path = config->unmask.items[i];
		for (size_t j = 0; j < N_ELEMENTS(path_lists); j++) {
			json_object *paths = js_get(linux_config, path_lists[j]);
			if (!paths)
				continue;

			for (size_t k = json_object_array_length(paths); k > 0; k--)
				if (!strcmp(path, "ALL") ||
				    !strcmp(path, js_array_string(paths, k - 1)))
					json_object_array_del_idx(paths, k - 1, 1);
		}
	}

	configure_paths(oci, config);
}
