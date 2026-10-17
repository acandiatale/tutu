package com.example.members.service;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import org.junit.jupiter.api.Test;

import com.example.members.web.MemberRequest;

class MemberRequestTest {

    @Test
    void lombokGeneratesAccessors() {
        MemberRequest r = new MemberRequest();
        r.setEmail("x@example.com");
        r.setName("x");
        assertEquals("x@example.com", r.getEmail());
        assertTrue(r.toString().contains("x@example.com"));
    }
}
