/* SPDX-License-Identifier: LGPL-2.1-or-later
 * Copyright (C) 2026 Alexander Larsson
 * See COPYING for the full license text.
 */

#include "qm-config.h"
#include "qm-utils.h"

#include <stdio.h>

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
	if (config->rootfs)
		js_set_string(result, "rootfs", config->rootfs);
	if (config->etc_dir)
		js_set_string(result, "etc-dir", config->etc_dir);
	if (config->var_dir)
		js_set_string(result, "var-dir", config->var_dir);
	if (config->seccomp_file)
		js_set_string(result, "seccomp", config->seccomp_file);
	js_set_string(result, "resolv-conf", config->resolv_conf);
	js_set_string(result, "userns", config->userns == USERNS_AUTO ? "auto" :
		      config->userns == USERNS_OFF ? "off" : "required");
	js_object_add(result, "verbose", js_new_bool(config->verbose));
	puts(json_object_to_json_string(result));
	return 0;
}
