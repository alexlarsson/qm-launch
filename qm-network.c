/* SPDX-License-Identifier: LGPL-2.1-or-later
 * Copyright (C) 2026 Alexander Larsson
 * See COPYING for the full license text.
 */

#define _GNU_SOURCE
#include "qm-network.h"
#include "qm-utils.h"

#include <arpa/inet.h>
#include <err.h>
#include <errno.h>
#include <fcntl.h>
#include <libmnl/libmnl.h>
#include <linux/if_link.h>
#include <linux/netfilter/nf_tables.h>
#include <linux/rtnetlink.h>
#include <linux/veth.h>
#include <net/if.h>
#include <nftables/libnftables.h>
#include <sched.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/time.h>

struct NetworkState {
	char *owner;
	char *bridge;
	char *veth;
	char *nft_table;
};

/* Requests contain interface names plus an alias of at most 255 bytes. */
typedef union {
	struct nlmsghdr alignment;
	char data[4096];
} Message;

typedef struct {
	struct mnl_socket *socket;
	unsigned int sequence;
} Rtnl;

static void rtnl_close(Rtnl *nl)
{
	if (nl->socket)
		mnl_socket_close(nl->socket);
}

#define autortnl __attribute__((cleanup(rtnl_close)))

static int rtnl_open(Rtnl *nl)
{
	nl->socket = mnl_socket_open2(NETLINK_ROUTE, SOCK_CLOEXEC);
	if (!nl->socket ||
	    mnl_socket_bind(nl->socket, 0, MNL_SOCKET_AUTOPID) < 0)
		return -1;
	struct timeval timeout = {.tv_sec = 5};
	return setsockopt(mnl_socket_get_fd(nl->socket), SOL_SOCKET,
			  SO_RCVTIMEO, &timeout, sizeof(timeout));
}

static struct nlmsghdr *message_init(Message *message, unsigned int type,
				     unsigned int flags)
{
	struct nlmsghdr *header = mnl_nlmsg_put_header(message->data);
	header->nlmsg_type = type;
	header->nlmsg_flags = NLM_F_REQUEST | flags;
	return header;
}

static int rtnl_request(Rtnl *nl, struct nlmsghdr *header, mnl_cb_t callback,
			void *data)
{
	header->nlmsg_seq = ++nl->sequence;
	if (mnl_socket_sendto(nl->socket, header, header->nlmsg_len) < 0)
		return -1;
	char response[32768];
	int result;
	do {
		ssize_t length = TEMP_FAILURE_RETRY(mnl_socket_recvfrom(
			nl->socket, response, sizeof(response)));
		if (length < 0)
			return -1;
		result = mnl_cb_run(response, length, nl->sequence,
				    mnl_socket_get_portid(nl->socket), callback,
				    data);
	} while (result > 0);
	return result;
}

typedef struct {
	int index;
	uint32_t mtu;
	uint32_t group;
	char alias[256];
} Link;

/* Hash the owner into a nonzero interface group, set during link creation.
 * Cleanup uses it only when the full owner alias is empty, covering a crash
 * before the separate alias update.
 */
static uint32_t owner_group(const char *owner)
{
	uint32_t hash = 2166136261U;
	for (const unsigned char *p = (const unsigned char *)owner; *p; p++)
		hash = (hash ^ *p) * 16777619U;
	return hash ? hash : 1;
}

static int link_reply(const struct nlmsghdr *header, void *data)
{
	Link *link = data;
	if (header->nlmsg_type != RTM_NEWLINK ||
	    mnl_nlmsg_get_payload_len(header) < sizeof(struct ifinfomsg)) {
		errno = EPROTO;
		return MNL_CB_ERROR;
	}
	struct ifinfomsg *info = mnl_nlmsg_get_payload(header);
	link->index = info->ifi_index;
	const struct nlattr *attr;
	mnl_attr_for_each(attr, header, sizeof(*info))
	{
		if (mnl_attr_get_type(attr) == IFLA_MTU ||
		    mnl_attr_get_type(attr) == IFLA_GROUP) {
			if (mnl_attr_validate(attr, MNL_TYPE_U32) < 0)
				return MNL_CB_ERROR;
			if (mnl_attr_get_type(attr) == IFLA_MTU)
				link->mtu = mnl_attr_get_u32(attr);
			else
				link->group = mnl_attr_get_u32(attr);
		} else if (mnl_attr_get_type(attr) == IFLA_IFALIAS) {
			if (mnl_attr_validate(attr, MNL_TYPE_NUL_STRING) < 0 ||
			    mnl_attr_get_payload_len(attr) >
				    sizeof(link->alias)) {
				errno = EPROTO;
				return MNL_CB_ERROR;
			}
			memcpy(link->alias, mnl_attr_get_str(attr),
			       mnl_attr_get_payload_len(attr));
		}
	}
	return MNL_CB_OK;
}

static int get_link(Rtnl *nl, const char *name, int index, Link *link)
{
	*link = (Link){0};
	Message message;
	struct nlmsghdr *header =
		message_init(&message, RTM_GETLINK, NLM_F_ACK);
	struct ifinfomsg *info =
		mnl_nlmsg_put_extra_header(header, sizeof(*info));
	info->ifi_family = AF_UNSPEC;
	info->ifi_index = index;
	if (name)
		mnl_attr_put_strz(header, IFLA_IFNAME, name);
	int result = rtnl_request(nl, header, link_reply, link);
	if (result < 0 && errno == ENODEV)
		return 0;
	return result;
}

typedef struct {
	int index;
	uint32_t metric;
} DefaultRoute;

static int route_reply(const struct nlmsghdr *header, void *data)
{
	DefaultRoute *route = data;
	if (mnl_nlmsg_get_payload_len(header) < sizeof(struct rtmsg)) {
		errno = EPROTO;
		return MNL_CB_ERROR;
	}
	struct rtmsg *info = mnl_nlmsg_get_payload(header);
	if (info->rtm_family != AF_INET || info->rtm_dst_len ||
	    info->rtm_table != RT_TABLE_MAIN || info->rtm_type != RTN_UNICAST)
		return MNL_CB_OK;
	uint32_t metric = 0;
	int index = 0;
	const struct nlattr *attr;
	mnl_attr_for_each(attr, header, sizeof(*info))
	{
		unsigned int type = mnl_attr_get_type(attr);
		if (type != RTA_OIF && type != RTA_PRIORITY)
			continue;
		if (mnl_attr_validate(attr, MNL_TYPE_U32) < 0)
			return MNL_CB_ERROR;
		if (type == RTA_OIF)
			index = mnl_attr_get_u32(attr);
		else
			metric = mnl_attr_get_u32(attr);
	}
	if (index && (!route->index || metric < route->metric))
		*route = (DefaultRoute){.index = index, .metric = metric};
	return MNL_CB_OK;
}

static int default_mtu(Rtnl *nl, uint32_t fallback, uint32_t *mtu)
{
	Message message;
	struct nlmsghdr *header =
		message_init(&message, RTM_GETROUTE, NLM_F_DUMP);
	struct rtmsg *info = mnl_nlmsg_put_extra_header(header, sizeof(*info));
	info->rtm_family = AF_INET;
	DefaultRoute route = {0};
	if (rtnl_request(nl, header, route_reply, &route) < 0)
		return -1;
	*mtu = fallback;
	if (route.index) {
		Link link;
		if (get_link(nl, NULL, route.index, &link) < 0)
			return -1;
		if (link.mtu)
			*mtu = link.mtu;
	}
	return 0;
}

static int create_bridge(Rtnl *nl, const NetworkOptions *options,
			 const char *owner, uint32_t mtu)
{
	Message message;
	struct nlmsghdr *header = message_init(
		&message, RTM_NEWLINK, NLM_F_ACK | NLM_F_CREATE | NLM_F_EXCL);
	mnl_nlmsg_put_extra_header(header, sizeof(struct ifinfomsg));
	mnl_attr_put_strz(header, IFLA_IFNAME, options->bridge);
	/* Unlike aliases, the group is applied during link creation. It covers
	 * recovery if we are killed before the subsequent alias update.
	 */
	mnl_attr_put_u32(header, IFLA_GROUP, owner_group(owner));
	mnl_attr_put_u32(header, IFLA_MTU, mtu);
	/* A fixed MAC keeps host ARP entries valid across restarts. */
	mnl_attr_put(header, IFLA_ADDRESS, sizeof(options->bridge_mac),
		     options->bridge_mac);
	struct nlattr *kind = mnl_attr_nest_start(header, IFLA_LINKINFO);
	mnl_attr_put_strz(header, IFLA_INFO_KIND, "bridge");
	mnl_attr_nest_end(header, kind);
	return rtnl_request(nl, header, NULL, NULL);
}

static int create_veth(Rtnl *nl, const NetworkOptions *options,
		       const char *owner, int bridge, int netns, uint32_t mtu)
{
	Message message;
	struct nlmsghdr *header = message_init(
		&message, RTM_NEWLINK, NLM_F_ACK | NLM_F_CREATE | NLM_F_EXCL);
	mnl_nlmsg_put_extra_header(header, sizeof(struct ifinfomsg));
	mnl_attr_put_strz(header, IFLA_IFNAME, options->veth);
	mnl_attr_put_u32(header, IFLA_GROUP, owner_group(owner));
	mnl_attr_put_u32(header, IFLA_MTU, mtu);
	mnl_attr_put_u32(header, IFLA_MASTER, bridge);
	struct nlattr *kind = mnl_attr_nest_start(header, IFLA_LINKINFO);
	mnl_attr_put_strz(header, IFLA_INFO_KIND, "veth");
	struct nlattr *details = mnl_attr_nest_start(header, IFLA_INFO_DATA);
	struct nlattr *peer = mnl_attr_nest_start(header, VETH_INFO_PEER);
	mnl_nlmsg_put_extra_header(header, sizeof(struct ifinfomsg));
	mnl_attr_put_strz(header, IFLA_IFNAME, options->interface);
	mnl_attr_put_u32(header, IFLA_NET_NS_FD, netns);
	mnl_attr_put_u32(header, IFLA_MTU, mtu);
	mnl_attr_nest_end(header, peer);
	mnl_attr_nest_end(header, details);
	mnl_attr_nest_end(header, kind);
	return rtnl_request(nl, header, NULL, NULL);
}

static int link_change(Rtnl *nl, int index, bool remove)
{
	Message message;
	struct nlmsghdr *header = message_init(
		&message, remove ? RTM_DELLINK : RTM_NEWLINK, NLM_F_ACK);
	struct ifinfomsg *info =
		mnl_nlmsg_put_extra_header(header, sizeof(*info));
	info->ifi_index = index;
	if (!remove) {
		info->ifi_flags = IFF_UP;
		info->ifi_change = IFF_UP;
	}
	return rtnl_request(nl, header, NULL, NULL);
}

static int set_alias(Rtnl *nl, int index, const char *owner)
{
	Message message;
	struct nlmsghdr *header =
		message_init(&message, RTM_SETLINK, NLM_F_ACK);
	struct ifinfomsg *info =
		mnl_nlmsg_put_extra_header(header, sizeof(*info));
	info->ifi_index = index;
	mnl_attr_put(header, IFLA_IFALIAS, strlen(owner), owner);
	return rtnl_request(nl, header, NULL, NULL);
}

static int add_address(Rtnl *nl, int index, const char *address,
		       unsigned int prefix_len)
{
	Message message;
	struct nlmsghdr *header = message_init(
		&message, RTM_NEWADDR, NLM_F_ACK | NLM_F_CREATE | NLM_F_EXCL);
	struct ifaddrmsg *info =
		mnl_nlmsg_put_extra_header(header, sizeof(*info));
	info->ifa_family = AF_INET;
	info->ifa_prefixlen = prefix_len;
	info->ifa_index = index;
	struct in_addr ip;
	if (inet_pton(AF_INET, address, &ip) != 1) {
		errno = EINVAL;
		return -1;
	}
	mnl_attr_put(header, IFA_LOCAL, sizeof(ip), &ip);
	mnl_attr_put(header, IFA_ADDRESS, sizeof(ip), &ip);
	return rtnl_request(nl, header, NULL, NULL);
}

static int add_default_route(Rtnl *nl, int index, const char *address)
{
	Message message;
	struct nlmsghdr *header = message_init(
		&message, RTM_NEWROUTE, NLM_F_ACK | NLM_F_CREATE | NLM_F_EXCL);
	struct rtmsg *info = mnl_nlmsg_put_extra_header(header, sizeof(*info));
	info->rtm_family = AF_INET;
	info->rtm_table = RT_TABLE_MAIN;
	info->rtm_protocol = RTPROT_STATIC;
	info->rtm_scope = RT_SCOPE_UNIVERSE;
	info->rtm_type = RTN_UNICAST;
	struct in_addr gateway;
	if (inet_pton(AF_INET, address, &gateway) != 1) {
		errno = EINVAL;
		return -1;
	}
	mnl_attr_put(header, RTA_GATEWAY, sizeof(gateway), &gateway);
	mnl_attr_put_u32(header, RTA_OIF, index);
	return rtnl_request(nl, header, NULL, NULL);
}

static int interface_sysctls(const NetworkOptions *options, const char *name)
{
	autofree char *rp_filter =
		xasprintf("net/ipv4/conf/%s/rp_filter", name);
	autofree char *disable_ipv6 =
		xasprintf("net/ipv6/conf/%s/disable_ipv6", name);
	autofree char *filter = xasprintf("%u", options->rp_filter);
	return sysctl_write(rp_filter, filter, false) ||
	       sysctl_write(disable_ipv6, options->disable_ipv6 ? "1" : "0", true);
}

static int configure_namespace(const NetworkOptions *options, int netns)
{
	pid_t child = fork();
	if (child < 0)
		return -1;
	if (!child) {
		autortnl Rtnl nl = {0};
		Link eth, lo;
		int result = setns(netns, CLONE_NEWNET) < 0 ||
			     rtnl_open(&nl) < 0 ||
			     get_link(&nl, options->interface, 0, &eth) < 0 || !eth.index ||
			     get_link(&nl, "lo", 0, &lo) < 0 || !lo.index ||
			     interface_sysctls(options, options->interface) ||
			     add_address(&nl, eth.index, options->ip,
					 options->prefix_len) < 0 ||
			     link_change(&nl, lo.index, false) < 0 ||
			     link_change(&nl, eth.index, false) < 0 ||
			     add_default_route(&nl, eth.index, options->gateway) < 0;
		if (result)
			warn("configure QM network namespace");
		_exit(result != 0);
	}
	return wait_child(child);
}

static void nft_cleanup(struct nft_ctx **ctx)
{
	if (*ctx)
		nft_ctx_free(*ctx);
}

#define autonft __attribute__((cleanup(nft_cleanup)))

static struct nft_ctx *nft_open(void)
{
	struct nft_ctx *ctx = nft_ctx_new(NFT_CTX_DEFAULT);
	if (!ctx)
		oom();
	if (nft_ctx_buffer_error(ctx) || nft_ctx_buffer_output(ctx))
		oom();
	nft_ctx_output_set_flags(ctx, NFT_CTX_OUTPUT_JSON);
	return ctx;
}

static int nft_command(struct nft_ctx *ctx, const char *command)
{
	if (nft_run_cmd_from_buffer(ctx, command)) {
		warnx("nftables: %s", nft_ctx_get_error_buffer(ctx));
		return -1;
	}
	return 0;
}

/* Return 0 if absent, 1 if ours, 2 if another owner, or -1 on error. */
static int nft_owner(struct nft_ctx *ctx, const char *name, const char *owner)
{
	if (nft_command(ctx, "list tables ip\n"))
		return -1;
	autofreejson json_object *output =
		json_tokener_parse(nft_ctx_get_output_buffer(ctx));
	if (!output) {
		warnx("invalid nftables response");
		return -1;
	}
	json_object *objects = js_need(output, "nftables", json_type_array);
	for (size_t i = 0; i < json_object_array_length(objects); i++) {
		json_object *table =
			js_get(json_object_array_get_idx(objects, i), "table");
		if (!table || strcmp(js_string(table, "name"), name))
			continue;
		json_object *comment = js_get(table, "comment");
		return comment && !strcmp(json_object_get_string(comment),
					  owner)
			       ? 1
			       : 2;
	}
	return 0;
}

static int nft_setup(struct nft_ctx *ctx, const NetworkOptions *options,
		     const char *owner)
{
	autofreejson json_object *quoted = js_new_string(owner);
	/* JSON preserves arbitrary path characters in the ownership comment. */
	autofree char *table = xasprintf(
		"{\"nftables\":[{\"create\":{\"table\":{\"family\":\"ip\","
		"\"name\":\"%s\",\"comment\":%s}}}]}", options->nft_table,
		json_object_to_json_string_ext(quoted, JSON_C_TO_STRING_PLAIN));
	if (nft_command(ctx, table))
		return -1;
	autofree char *subnet = xasprintf("%s/%u", options->subnet,
					options->prefix_len);
	autofree char *rules = xasprintf(
		"add chain ip %s forward "
		"{ type filter hook forward priority filter; policy accept; }\n"
		"add rule ip %s forward "
		"oifname \"%s\" ct state established,related accept\n"
		"add rule ip %s forward iifname \"%s\" ip saddr %s accept\n"
		"add rule ip %s forward iifname \"%s\" drop\n"
		"add rule ip %s forward oifname \"%s\" ip daddr %s accept\n"
		"add chain ip %s postrouting "
		"{ type nat hook postrouting priority srcnat; policy accept; "
		"}\n"
		"add rule ip %s postrouting "
		"ip saddr %s ip daddr != %s masquerade\n",
		options->nft_table,
		options->nft_table, options->bridge,
		options->nft_table, options->bridge, subnet,
		options->nft_table, options->bridge,
		options->nft_table, options->bridge, options->ip,
		options->nft_table,
		options->nft_table, subnet, subnet);
	/* One transaction installs all chains and rules in the owned table. */
	return nft_command(ctx, rules);
}

static char *network_owner(const char *runtime_dir)
{
	autofree char *canonical = realpath(runtime_dir, NULL);
	if (!canonical)
		return NULL;
	char *owner = xasprintf("qm-launch:%s", canonical);
	if (strlen(owner) >= sizeof(((Link *)0)->alias)) {
		free(owner);
		errno = ENAMETOOLONG;
		return NULL;
	}
	return owner;
}

static bool valid_interface(const char *name)
{
	const char *characters = "abcdefghijklmnopqrstuvwxyz"
				 "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-";
	return name && *name && strlen(name) < IFNAMSIZ &&
	       strspn(name, characters) == strlen(name);
}

static bool valid_table(const char *name)
{
	const char *characters = "abcdefghijklmnopqrstuvwxyz"
				 "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-";
	return name && *name && strlen(name) < NFT_TABLE_MAXNAMELEN &&
	       strspn(name, characters) == strlen(name) &&
	       !(*name >= '0' && *name <= '9') && *name != '.' && *name != '-';
}

/* Keep names within rtnetlink's bounds and safe as nftables identifiers. */
static int validate_options(const NetworkOptions *options)
{
	if (!valid_interface(options->bridge) || !valid_interface(options->veth) ||
	    !valid_interface(options->interface) || !valid_table(options->nft_table) ||
	    options->prefix_len > 32 || options->rp_filter > 2 ||
	    !options->fallback_mtu)
		goto invalid;
	const char *addresses[] = {options->ip, options->gateway, options->subnet};
	for (size_t i = 0; i < N_ELEMENTS(addresses); i++) {
		struct in_addr ip;
		if (!addresses[i] || inet_pton(AF_INET, addresses[i], &ip) != 1)
			goto invalid;
	}
	return 0;
invalid:
	warnx("invalid network options");
	return 1;
}

json_object *network_state_serialize(const NetworkState *state)
{
	json_object *object = js_new_object();
	js_set_string(object, "owner", state->owner);
	js_set_string(object, "bridge", state->bridge);
	js_set_string(object, "veth", state->veth);
	js_set_string(object, "nft_table", state->nft_table);
	return object;
}

NetworkState *network_state_deserialize(json_object *object)
{
	const char *owner = js_string(object, "owner");
	const char *bridge = js_string(object, "bridge");
	const char *veth = js_string(object, "veth");
	const char *nft_table = js_string(object, "nft_table");
	if (!valid_interface(bridge) || !valid_interface(veth) ||
	    !valid_table(nft_table) || !*owner ||
	    strlen(owner) >= sizeof(((Link *)0)->alias))
		errx(1, "invalid saved network state");
	NetworkState *state = xnew(NetworkState);
	*state = (NetworkState){
		.owner = xstrdup(owner),
		.bridge = xstrdup(bridge),
		.veth = xstrdup(veth),
		.nft_table = xstrdup(nft_table),
	};
	return state;
}

void network_state_free(NetworkState *state)
{
	if (!state)
		return;
	free(state->owner);
	free(state->bridge);
	free(state->veth);
	free(state->nft_table);
	free(state);
}

NetworkState *network_prepare(const NetworkOptions *options)
{
	if (validate_options(options))
		return NULL;
	const char *runtime_dir = options->runtime_dir;
	autofree char *owner = network_owner(runtime_dir);
	if (!owner) {
		warn("network owner for %s", runtime_dir);
		return NULL;
	}
	autortnl Rtnl nl = {0};
	if (rtnl_open(&nl) < 0) {
		warn("open rtnetlink");
		return NULL;
	}
	Link bridge, veth;
	if (get_link(&nl, options->bridge, 0, &bridge) < 0 ||
	    get_link(&nl, options->veth, 0, &veth) < 0) {
		warn("find QM network interfaces");
		return NULL;
	}
	if (bridge.index || veth.index) {
		warnx("%s or %s already exists; run --cleanup for its owner",
		      options->bridge, options->veth);
		return NULL;
	}
	autonft struct nft_ctx *nft = nft_open();
	int table = nft_owner(nft, options->nft_table, owner);
	if (table < 0)
		return NULL;
	if (table) {
		warnx("nftables table %s already exists; run --cleanup for its "
		      "owner",
		      options->nft_table);
		return NULL;
	}
	NetworkState *state = xnew(NetworkState);
	*state = (NetworkState){
		.owner = steal_ptr(&owner),
		.bridge = xstrdup(options->bridge),
		.veth = xstrdup(options->veth),
		.nft_table = xstrdup(options->nft_table),
	};
	return state;
}

int network_setup(const NetworkOptions *options, const NetworkState *state)
{
	const char *owner = state->owner;
	autortnl Rtnl nl = {0};
	if (rtnl_open(&nl) < 0) {
		warn("open rtnetlink");
		return 1;
	}
	autonft struct nft_ctx *nft = nft_open();
	uint32_t mtu;
	if (default_mtu(&nl, options->fallback_mtu, &mtu) < 0) {
		warn("find default route MTU");
		return 1;
	}
	autofree char *netns = join_path(options->runtime_dir, "netns");
	autoclose int ns =
		TEMP_FAILURE_RETRY(open(netns, O_RDONLY | O_CLOEXEC));
	if (ns < 0) {
		warn("open %s", netns);
		return 1;
	}
	Link bridge, veth;
	autofree char *forwarding =
		xasprintf("net/ipv4/conf/%s/forwarding", options->bridge);
	if (create_bridge(&nl, options, owner, mtu) < 0 ||
	    get_link(&nl, options->bridge, 0, &bridge) < 0 ||
	    set_alias(&nl, bridge.index, owner) < 0 ||
	    sysctl_write("net/ipv4/ip_forward", "1", false) ||
	    sysctl_write(forwarding, "1", false) ||
	    interface_sysctls(options, options->bridge) ||
	    add_address(&nl, bridge.index, options->gateway,
			options->prefix_len) < 0 ||
	    create_veth(&nl, options, owner, bridge.index, ns, mtu) < 0 ||
	    get_link(&nl, options->veth, 0, &veth) < 0 ||
	    set_alias(&nl, veth.index, owner) < 0 ||
	    interface_sysctls(options, options->veth)) {
		warn("set up QM network");
		return 1;
	}
	if (configure_namespace(options, ns) || nft_setup(nft, options, owner))
		return 1;
	if (link_change(&nl, bridge.index, false) < 0 ||
	    link_change(&nl, veth.index, false) < 0) {
		warn("bring up QM network interfaces");
		return 1;
	}
	return 0;
}

static int remove_link(Rtnl *nl, const char *name, const char *owner)
{
	Link link;
	if (get_link(nl, name, 0, &link) < 0) {
		warn("find %s", name);
		return 1;
	}
	if (!link.index)
		return 0;
	if (link.alias[0] ? strcmp(link.alias, owner)
			  : link.group != owner_group(owner)) {
		warnx("%s belongs to another owner; leaving it alone", name);
		return 1;
	}
	if (link_change(nl, link.index, true) < 0 && errno != ENODEV) {
		warn("remove %s", name);
		return 1;
	}
	return 0;
}

int network_teardown(const NetworkState *state)
{
	if (!state)
		return 0;
	const char *owner = state->owner;
	const char *bridge = state->bridge;
	const char *veth = state->veth;
	const char *nft_table = state->nft_table;
	int result = 0;
	autonft struct nft_ctx *nft = nft_open();
	int table = nft_owner(nft, nft_table, owner);
	if (table < 0)
		result = 1;
	else if (table == 1) {
		autofree char *command = xasprintf("delete table ip %s\n",
						 nft_table);
		result |= nft_command(nft, command) != 0;
	} else if (table == 2) {
		warnx("nftables table %s belongs to another owner; leaving it "
		      "alone",
		      nft_table);
		result = 1;
	}
	autortnl Rtnl nl = {0};
	if (rtnl_open(&nl) < 0) {
		warn("open rtnetlink for cleanup");
		result = 1;
	} else {
		result |= remove_link(&nl, veth, owner);
		result |= remove_link(&nl, bridge, owner);
	}
	return result;
}
