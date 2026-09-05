#!/usr/bin/env bash
# Shared cluster environment for exact multi-node GB10 specs.
#
# Runtime defaults are part of the launch contract. Any changed recipe
# requires a new candidate and fresh qualification before catalog promotion.
#
# A confirmed .cluster-topology.json supplies per-rank control IPs, SSH targets,
# HCAs, and interfaces. HEAD_IP/WORKER_IP environment variables never construct
# multi-node topology; membership comes from the confirmed manifest only.

_cluster_env_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "$_cluster_env_dir/topology.sh"

# ---- topology ---------------------------------------------------------------
export MASTER_PORT="${MASTER_PORT:-29500}"

# ---- NCCL defaults ---------------------------------------------------------
# Per-rank confirmed fabric endpoints replace these defaults at launch.
export NCCL_IB_HCA="${NCCL_IB_HCA:-rocep1s0f0,roceP2p1s0f0}"
# Preserve this value unless the selected spec explicitly changes it.
export NCCL_IB_QPS_PER_CONNECTION="${NCCL_IB_QPS_PER_CONNECTION:-4}"
# Confirmed manifests supply per-rank interfaces at launch.
export NCCL_SOCKET_IFNAME="${NCCL_SOCKET_IFNAME:-enp1s0f0np0}"
export GLOO_SOCKET_IFNAME="${GLOO_SOCKET_IFNAME:-enp1s0f0np0}"
export TP_SOCKET_IFNAME="${TP_SOCKET_IFNAME:-enp1s0f0np0}"
export NCCL_IB_DISABLE=0
export NCCL_DEBUG="${NCCL_DEBUG:-WARN}"
# Deliberately NOT set globally:
#   NCCL_IB_GID_INDEX   - auto-detect picks the RoCEv2 GID correctly
#   NCCL_NET_GDR_LEVEL  - GDR is off on GB10 (separate PCIe root complexes)
#   MTU 9000            - +0.7..1.5% only; PCIe Gen5 x4 is the bottleneck
# Multi-node launch sets NCCL_NET=IB so a broken RDMA path fails closed instead
# of silently moving model traffic onto the shared control LAN.

# ---- vLLM on unified memory -------------------------------------------------
# GB10 has no dedicated VRAM; CUDA, OS, and page cache share 121 GiB.
# GPU memory utilization belongs to the selected immutable recipe.
# Per-rank VLLM_HOST_IP is set at launch from the confirmed manifest.
