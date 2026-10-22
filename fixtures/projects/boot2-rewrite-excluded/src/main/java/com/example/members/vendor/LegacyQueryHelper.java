package com.example.members.vendor;

/**
 * "Vendored" helper copied from another project: fully-qualified JPA type names (javax namespace), no imports.
 * This directory is under openrewrite.exclusions (sbmig.e2e.yml), so the jakarta recipes never see it — the
 * jakarta-namespace-sweep handler must rename the references (E13).
 */
public final class LegacyQueryHelper {

    private LegacyQueryHelper() {
    }

    public static long count(javax.persistence.EntityManager em, String entity) {
        javax.persistence.Query q = em.createQuery("select count(m) from " + entity + " m");
        return ((Number) q.getSingleResult()).longValue();
    }
}
