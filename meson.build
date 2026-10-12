# SPDX-License-Identifier: LGPL-2.1-or-later
# Copyright (C) 2026 Alexander Larsson
# See COPYING for the full license text.

project('qm-launch', 'c', version: '0.1.0',
  default_options: ['c_std=gnu17', 'warning_level=3', 'werror=true'])

data_dir = get_option('prefix') / get_option('datadir') / 'qm'
build_config = configuration_data()
build_config.set_quoted('QM_DATA_DIR', data_dir)
configure_file(output: 'config.h', configuration: build_config)

launcher = executable('qm-launch', ['qm-launch.c', 'qm-utils.c'],
  dependencies: dependency('json-c'), install: true)

service_config = configuration_data()
service_config.set('BINDIR', get_option('prefix') / get_option('bindir'))
service_config.set('DATADIR', data_dir)
foreach unit : ['qm2.service', 'qm2-resolv-conf.path', 'qm2-resolv-conf.service']
  configure_file(input: unit + '.in', output: unit,
    configuration: service_config,
    install_dir: get_option('prefix') / 'lib/systemd/system')
endforeach
install_data('qm.json',
  install_dir: data_dir)

python = find_program('python3', required: false)
if python.found()
  prepare_launcher = executable('test-prepare',
    ['qm-launch.c', 'qm-utils.c', 'tests/prepare-cgroup.c', 'tests/file-truncate.c'],
    include_directories: include_directories('.'),
    dependencies: dependency('json-c'),
    # Keep LTO from inlining the call that the fixture wraps.
    c_args: ['-fno-lto'],
    link_args: ['-fno-lto', '-Wl,--wrap=get_current_cgroup', '-Wl,--wrap=ftruncate64'])
  test('prepare', python, args: [files('tests/prepare.py'), prepare_launcher,
    meson.current_source_dir(), '/payload'])
  test('exec', python, args: [files('tests/exec.py'), launcher])
endif

utils_test = executable('test-utils', ['tests/utils.c', 'qm-utils.c'],
  include_directories: include_directories('.'),
  dependencies: dependency('json-c'),
  link_args: ['-Wl,--wrap=open64', '-Wl,--wrap=read', '-Wl,--wrap=__read_chk',
    '-Wl,--wrap=write', '-Wl,--wrap=waitpid', '-Wl,--wrap=fork'])
test('utils', utils_test)
