package com.example.catalog.service;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public class CatalogServiceTest {

    @Test
    public void clampsPageSize() {
        assertEquals(1, CatalogService.clampPageSize(0, 50));
        assertEquals(20, CatalogService.clampPageSize(20, 50));
        assertEquals(50, CatalogService.clampPageSize(80, 50));
    }

    @Test
    public void exportDirEndsWithExport() {
        assertTrue(CatalogService.exportDir().getPath().endsWith("export"));
    }
}
