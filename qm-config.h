/* SPDX-License-Identifier: LGPL-2.1-or-later
 * Copyright (C) 2026 Alexander Larsson
 * See COPYING for the full license text.
 */

#pragma once

#include <stdbool.h>

typedef enum { USERNS_AUTO, USERNS_OFF, USERNS_REQUIRED } UserNamespaceMode;

typedef struct {
	char *base_config;
	char *hostname;
	char *rootfs;
	char *etc_dir;
	char *var_dir;
	char *seccomp_file;
	char *resolv_conf;
	UserNamespaceMode userns;
	bool verbose;
} Config;

void config_free(Config *config);

static inline void cleanup_config(Config **config)
{
	config_free(*config);
}

#define autofreeconfig __attribute__((cleanup(cleanup_config)))

/* Load qmc.conf, then sorted .conf files from qmc.conf.d, in each directory in turn.
 * Missing files/directories are optional. Later keys replace earlier values.
 * An explicit filename instead loads only that file, which must exist.
 * Returns an allocated Config with defaults and owned strings; free with config_free.
 * Failures report the offending file and exit.
 */
Config *configuration_load(const char *vendor_dir, const char *admin_dir,
				const char *filename);
