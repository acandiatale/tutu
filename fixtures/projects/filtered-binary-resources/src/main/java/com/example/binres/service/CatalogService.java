package com.example.binres.service;

import java.io.IOException;
import java.io.InputStream;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

/** Reads the binary catalog (data/items.dmdb) that Maven must copy byte for byte. */
@Service
public class CatalogService {

    @Value("${app.version}")
    private String version;

    public String version() {
        return version;
    }

    public int catalogSize() throws IOException {
        try (InputStream in = getClass().getResourceAsStream("/data/items.dmdb")) {
            return in == null ? -1 : in.readAllBytes().length;
        }
    }
}
