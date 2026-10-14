# SPDX-License-Identifier: LGPL-2.1-or-later
# Copyright (C) 2026 Alexander Larsson
# See COPYING for the full license text.

project('qm-launch', 'c', version: '0.1.1',
  default_options: ['c_std=gnu17', 'warning_level=3', 'werror=true'])

data_dir = get_option('prefix') / get_option('datadir') / 'qm'
build_config = configuration_data()
build_config.set_quoted('QM_DATA_DIR', data_dir)
build_config.set_quoted('QM_CONFIG_DIR', get_option('prefix') / get_option('sysconfdir'))
configure_file(output: 'config.h', configuration: build_config)

launcher_sources = [
  'qmc.c',
  'qm-utils.c',
  'qm-network.c',
  'qm-state.c',
  'qm-console.c',
  'qm-config.c',
  'qm-seccomp.c',
]
launcher_deps = [dependency('json-c'), dependency('libmnl'), dependency('libnftables')]
launcher = executable('qmc', launcher_sources,
  dependencies: launcher_deps, install: true)

service_config = configuration_data()
service_config.set('BINDIR', get_option('prefix') / get_option('bindir'))
service_config.set('DATADIR', data_dir)
foreach unit : ['qmc.service', 'qmc-resolv-conf.path', 'qmc-resolv-conf.service']
  configure_file(input: unit + '.in', output: unit,
    configuration: service_config,
    install_dir: get_option('prefix') / 'lib/systemd/system')
endforeach
configure_file(input: 'qmc.conf.in', output: 'qmc.conf',
  configuration: service_config, install_dir: data_dir)
install_emptydir(data_dir / 'qmc.conf.d')
install_emptydir(get_option('prefix') / get_option('sysconfdir') / 'qmc.conf.d')
install_data('qm.json',
  install_dir: data_dir)
install_data('00-qm-forwarding.conf', '90-qm-launch.conf',
  install_dir: get_option('prefix') / 'lib/sysctl.d')

python = find_program('python3', required: false)
if python.found()
  config_test = executable('test-config', ['tests/config.c', 'qm-config.c', 'qm-utils.c'],
    include_directories: include_directories('.'),
    dependencies: dependency('json-c'))
  test('config', python, args: [files('tests/config.py'), config_test, data_dir])
  executable('test-launch-network', launcher_sources + ['tests/network-nft.c'],
    include_directories: include_directories('.'),
    dependencies: launcher_deps,
    link_args: ['-Wl,--wrap=nft_run_cmd_from_buffer', '-Wl,--wrap=mnl_socket_sendto',
      '-Wl,--wrap=sysctl_write'])
  executable('test-network', ['tests/network.c', 'qm-network.c', 'qm-utils.c', 'qm-state.c'],
    include_directories: include_directories('.'),
    dependencies: launcher_deps)
  prepare_launcher = executable('test-prepare',
    launcher_sources + ['tests/prepare-cgroup.c', 'tests/prepare-machine-id.c', 'tests/file-truncate.c'],
    include_directories: include_directories('.'),
    dependencies: launcher_deps,
    # Keep LTO from inlining the call that the fixture wraps.
    c_args: ['-fno-lto'],
    link_args: ['-fno-lto', '-Wl,--wrap=get_current_cgroup', '-Wl,--wrap=read_text_file', '-Wl,--wrap=ftruncate64'])
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
