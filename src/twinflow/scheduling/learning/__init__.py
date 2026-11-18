"""K learning for ATCS (COMP-025, REQ-122, REQ-123, D-082, Pinedo Example 18.2.1 p.488-489).

`ktable` holds the `KTable` model, the p.376 seed table, the inverse-distance lookup and the
atomic JSON store; `perturb` runs ATCS at a center's looked-up K and its 8 neighbors and
updates the table purely. ATCS is reached through the registry by name at call time; this
package never imports an algorithm module.
"""
