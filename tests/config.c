/* SPDX-License-Identifier: LGPL-2.1-or-later
 * Copyright (C) 2026 Alexander Larsson
 * See COPYING for the full license text.
 */

#include "qm-config.h"
#include "qm-utils.h"

#include <stdio.h>

static json_object *path_operations_json(const PtrArray *operations)
{
	autofreejson json_object *result = js_new_array();
	for (size_t i = 0; i < operations->length; i++) {
		const PathOperation *operation = operations->items[i];
		autofreejson json_object *entry = js_new_object();
		js_set_string(entry, "kind", operation->device ? "devices" : "mounts");
		if (operation->destination)
			js_set_string(entry, "destination", operation->destination);
		if (operation->seat)
			js_set_string(entry, "seat", operation->seat);
		if (operation->source)
			js_set_string(entry, "source", operation->source);
		if (operation->remove)
			js_object_add(entry, "remove", js_new_bool(true));
		if (operation->optional)
			js_object_add(entry, "optional", js_new_bool(true));

		if (operation->options_set) {
			autofreejson json_object *options = js_new_array();
			for (size_t j = 0; j < operation->options.length; j++)
				js_array_add(options,
					     js_new_string(operation->options.items[j]));
			js_object_add(entry, "options", steal_ptr(&options));
		}
		js_array_add(result, steal_ptr(&entry));
	}
	return steal_ptr(&result);
}

static json_object *capability_operations_json(const PtrArray *operations)
{
	autofreejson json_object *result = js_new_array();
	for (size_t i = 0; i < operations->length; i++) {
		const CapabilityOperation *operation = operations->items[i];
		autofreejson json_object *entry = js_new_object();
		js_set_string(entry, "name", operation->name);
		js_object_add(entry, "remove", js_new_bool(operation->remove));
		js_array_add(result, steal_ptr(&entry));
	}
	return steal_ptr(&result);
}

int main(int argc, char **argv)
{
	if (argc != 3 && argc != 4)
		return 2;
	autofreeconfig Config *config =
		configuration_load(argv[1], argv[2], argc == 4 ? argv[3] : NULL);
	/* Inspect the typed result rather than the JSON used to load it. */
	autofreejson json_object *result = js_new_object();
	js_set_string(result, "base-config", config->base_config);
	js_set_string(result, "hostname", config->hostname);
	js_set_string(result, "ip", config->ip);
	js_set_string(result, "gateway", config->gateway);
	js_object_add(result, "prefix-length", js_new_int(config->prefix_length));
	js_set_string(result, "subnet", config->subnet);
	if (config->rootfs)
		js_set_string(result, "rootfs", config->rootfs);
	if (config->etc_dir)
		js_set_string(result, "etc-dir", config->etc_dir);
	if (config->var_dir)
		js_set_string(result, "var-dir", config->var_dir);
	if (config->seccomp_file)
		js_set_string(result, "seccomp", config->seccomp_file);
	js_set_string(result, "resolv-conf", config->resolv_conf);
	js_set_string(result, "userns",
		      config->userns == USERNS_AUTO  ? "auto"
		      : config->userns == USERNS_OFF ? "off"
						     : "required");
	js_object_add(result, "verbose", js_new_bool(config->verbose));
	if (config->memory_max_set)
		js_object_add(result, "memory-max", js_new_int(config->memory_max));
	if (config->path_operations.length)
		js_object_add(result, "path-operations",
			      path_operations_json(&config->path_operations));
	if (config->capability_operations.length)
		js_object_add(result, "capability-operations",
			      capability_operations_json(&config->capability_operations));
	if (config->unmask.length) {
		autofreejson json_object *unmask = js_new_array();
		for (size_t i = 0; i < config->unmask.length; i++)
			js_array_add(unmask, js_new_string(config->unmask.items[i]));
		js_object_add(result, "unmask", steal_ptr(&unmask));
	}
	puts(json_object_to_json_string(result));
	return 0;
}
