package com.example.inventory.io;

import java.io.StringReader;
import java.io.StringWriter;

import javax.xml.bind.JAXBContext;
import javax.xml.bind.JAXBException;
import javax.xml.bind.Marshaller;
import javax.xml.bind.Unmarshaller;

import com.example.inventory.model.StockItem;

/**
 * JAXB based export of stock items (javax.xml.bind is part of the JDK up to Java 10).
 */
public class XmlExporter {

    private final JAXBContext context;

    public XmlExporter() {
        try {
            this.context = JAXBContext.newInstance(StockItem.class);
        } catch (JAXBException e) {
            throw new IllegalStateException("cannot initialise JAXB", e);
        }
    }

    public String toXml(StockItem item) {
        try {
            Marshaller marshaller = context.createMarshaller();
            marshaller.setProperty(Marshaller.JAXB_FORMATTED_OUTPUT, Boolean.TRUE);
            StringWriter out = new StringWriter();
            marshaller.marshal(item, out);
            return out.toString();
        } catch (JAXBException e) {
            throw new IllegalStateException("marshal failed", e);
        }
    }

    public StockItem fromXml(String xml) {
        try {
            Unmarshaller unmarshaller = context.createUnmarshaller();
            return (StockItem) unmarshaller.unmarshal(new StringReader(xml));
        } catch (JAXBException e) {
            throw new IllegalStateException("unmarshal failed", e);
        }
    }
}
