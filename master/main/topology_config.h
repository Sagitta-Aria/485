#pragma once

#define TOPOLOGY_MAX_MODULES 10U
#define TOPOLOGY_DEFAULT_MODULES 7U
#define TOPOLOGY_MAX_ROUNDS 16U
#define TOPOLOGY_GROUPS_PER_MODULE 24U

/* master1 has a fixed X0-Y0 .. X3-Y3 input path; master2 keeps its matrix open.
 * Slave same-name Y buses provide the selection network on both sides.
 */
