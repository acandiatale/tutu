package com.example.inventory;

import com.example.inventory.io.XmlExporter;
import com.example.inventory.model.StockItem;
import com.example.inventory.service.InventoryService;

/**
 * Batch entry point used by the nightly cron job.
 */
public class InventoryCli {

    public static void main(String[] args) {
        InventoryService service = new InventoryService();
        service.register("abc-1", "Widget", 10);
        service.register("xyz-2", "Gadget", 2);
        XmlExporter exporter = new XmlExporter();
        for (StockItem item : service.lowStock(5)) {
            System.out.println(exporter.toXml(item));
        }
    }
}
