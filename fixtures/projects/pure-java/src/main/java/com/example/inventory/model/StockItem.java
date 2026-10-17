package com.example.inventory.model;

import java.util.Date;

import javax.xml.bind.annotation.XmlAccessType;
import javax.xml.bind.annotation.XmlAccessorType;
import javax.xml.bind.annotation.XmlElement;
import javax.xml.bind.annotation.XmlRootElement;

import org.apache.commons.lang3.builder.ToStringBuilder;

/**
 * One SKU in a warehouse. Serialized to XML for the legacy ERP export.
 */
@XmlRootElement(name = "stockItem")
@XmlAccessorType(XmlAccessType.FIELD)
public class StockItem {

    @XmlElement(required = true)
    private String sku;

    @XmlElement
    private String name;

    @XmlElement
    private int quantity;

    @XmlElement
    private Date lastUpdated;

    public StockItem() {
        // required by JAXB
    }

    public StockItem(String sku, String name, int quantity, Date lastUpdated) {
        this.sku = sku;
        this.name = name;
        this.quantity = quantity;
        this.lastUpdated = lastUpdated;
    }

    public String getSku() {
        return sku;
    }

    public void setSku(String sku) {
        this.sku = sku;
    }

    public String getName() {
        return name;
    }

    public void setName(String name) {
        this.name = name;
    }

    public int getQuantity() {
        return quantity;
    }

    public void setQuantity(int quantity) {
        this.quantity = quantity;
    }

    public Date getLastUpdated() {
        return lastUpdated;
    }

    public void setLastUpdated(Date lastUpdated) {
        this.lastUpdated = lastUpdated;
    }

    @Override
    public String toString() {
        return new ToStringBuilder(this)
                .append("sku", sku)
                .append("name", name)
                .append("quantity", quantity)
                .toString();
    }
}
