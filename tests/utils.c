/* SPDX-License-Identifier: LGPL-2.1-or-later
 * Copyright (C) 2026 Alexander Larsson
 * See COPYING for the full license text.
 */

#define _GNU_SOURCE
#include "qm-utils.h"

#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/wait.h>

static unsigned open_calls, read_calls, write_calls, wait_calls;
static bool fail_fork;

pid_t __real_fork(void);
int __real_open64(const char *path, int flags, ...);
ssize_t __real_read(int fd, void *buffer, size_t size);
ssize_t __real_write(int fd, const void *buffer, size_t size);
pid_t __real_waitpid(pid_t pid, int *status, int options);

int __wrap_open64(const char *path, int flags, ...)
{
	if (++open_calls <= 2) {
		errno = EINTR;
		return -1;
	}

	mode_t mode = 0;
	if (flags & O_CREAT) {
		va_list args;
		va_start(args, flags);
		mode = va_arg(args, mode_t);
		va_end(args);
	}

	return __real_open64(path, flags, mode);
}

ssize_t __wrap_read(int fd, void *buffer, size_t size)
{
	if (++read_calls == 1 || read_calls == 3) {
		errno = EINTR;
		return -1;
	}

	return __real_read(fd, buffer, size > 3 ? 3 : size);
}

/* Fortified builds use this entry point instead of read. */
ssize_t __wrap___read_chk(int fd, void *buffer, size_t size, size_t buffer_size)
{
	assert(size <= buffer_size);
	return __wrap_read(fd, buffer, size);
}

ssize_t __wrap_write(int fd, const void *buffer, size_t size)
{
	if (++write_calls == 1 || write_calls == 3) {
		errno = EINTR;
		return -1;
	}

	return __real_write(fd, buffer, size > 3 ? 3 : size);
}

pid_t __wrap_waitpid(pid_t pid, int *status, int options)
{
	if (++wait_calls <= 2) {
		errno = EINTR;
		return -1;
	}

	return __real_waitpid(pid, status, options);
}

pid_t __wrap_fork(void)
{
	if (fail_fork) {
		errno = EAGAIN;
		return -1;
	}

	return __real_fork();
}

int main(void)
{
	char filename[] = "/tmp/qm-utils-XXXXXX";
	autoclose int fd = mkstemp(filename);
	assert(fd >= 0);

	const char *text = "interrupted and partial I/O";
	write_text_file(filename, text);
	assert(open_calls >= 3 && write_calls >= 4);

	open_calls = read_calls = 0;
	autofree char *contents = read_text_file(filename);
	assert(!strcmp(contents, text));
	assert(open_calls >= 3 && read_calls >= 4);

	autofreejson json_object *object = js_new_object();
	js_set_string(object, "message", text);
	open_calls = write_calls = 0;
	js_save(object, filename);
	assert(open_calls >= 3 && write_calls >= 4);

	open_calls = read_calls = 0;
	autofreejson json_object *loaded = js_load(filename);
	assert(json_object_equal(object, loaded));
	assert(open_calls >= 3 && read_calls >= 4);
	assert(unlink(filename) == 0);
	assert(try_read_text_file(filename) == NULL && errno == ENOENT);
	assert(try_read_text_file("/tmp") == NULL && errno == EISDIR);

	pid_t child = fork();
	assert(child >= 0);
	if (!child)
		_exit(17);

	int status = wait_child(child);
	assert(status == 17);
	assert(wait_calls == 3);

	errno = 0;
	status = wait_child(child);
	assert(status == -1 && errno == ECHILD);

	char *argv[] = {"/bin/true", NULL};
	fail_fork = true;
	status = run_command(argv);
	assert(status == -1 && errno == EAGAIN);

	fail_fork = false;
	status = run_command(argv);
	assert(status == 0);

	/* Interrupted/partial writes must publish a complete state, and the
	 * initial publication must not replace another launch's saved state.
	 */
	write_calls = 0;
	js_save_atomic(object, filename, false);
	struct stat before, after;
	assert(stat(filename, &before) == 0);
	assert((before.st_mode & 0777) == 0600);
	autofreejson json_object *replacement = js_new_object();
	js_set_string(replacement, "message", "replacement state");
	child = fork();
	assert(child >= 0);
	if (!child) {
		js_save_atomic(replacement, filename, false);
		_exit(0);
	}
	assert(wait_child(child) == 1);
	autofreejson json_object *original = js_load(filename);
	assert(json_object_equal(original, object));
	write_calls = 0;
	js_save_atomic(replacement, filename, true);
	assert(stat(filename, &after) == 0);
	assert(before.st_ino != after.st_ino);
	autofreejson json_object *replaced = js_load(filename);
	assert(json_object_equal(replaced, replacement));
	assert(unlink(filename) == 0);

	return 0;
}
