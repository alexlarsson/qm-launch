# SPDX-License-Identifier: LGPL-2.1-or-later
# Copyright (C) 2026 Alexander Larsson
# See COPYING for the full license text.

Name:           qm-launch
Version:        0.1.0
Release:        1%{?dist}
Summary:        Launch the QM container directly with crun

License:        LGPL-2.1-or-later
URL:            https://github.com/alexlarsson/qm-launch
Source0:        %{name}-%{version}.tar.xz

BuildRequires:  gcc
BuildRequires:  meson
BuildRequires:  pkgconfig(json-c)
BuildRequires:  python3
BuildRequires:  systemd-rpm-macros

# QM supplies the seccomp profile, rootfs setup and SELinux policy.
Requires:       qm
Requires:       crun
Requires:       netavark
Requires:       systemd >= 254
%{?systemd_requires}

%description
qm-launch prepares an OCI bundle and launches the QM container directly with
crun and Netavark. It provides qm2.service alongside QM's existing service
and supports running commands in the container with --exec.

%prep
%autosetup -p1

%build
%meson
%meson_build

%install
%meson_install

%check
%meson_test

%post
%systemd_post qm2.service qm2-resolv-conf.path qm2-resolv-conf.service

%preun
%systemd_preun qm2.service qm2-resolv-conf.path qm2-resolv-conf.service

%postun
%systemd_postun qm2.service qm2-resolv-conf.path qm2-resolv-conf.service

%files
%license COPYING
%doc README.md
%{_bindir}/qm-launch
%{_unitdir}/qm2.service
%{_unitdir}/qm2-resolv-conf.path
%{_unitdir}/qm2-resolv-conf.service
%{_datadir}/qm/qm.json

%changelog
* Tue Oct 13 2026 Alexander Larsson <alexl@redhat.com> - 0.1.0-1
- Initial package
