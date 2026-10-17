package com.example.board.service;

import static org.junit.Assert.assertEquals;

import org.junit.Test;

import com.example.board.dao.NoticeDao;

public class NoticeServiceTest {

    @Test
    public void postTrimsTitle() {
        NoticeService service = new NoticeService(new NoticeDao());
        assertEquals("hi", service.post("  hi ", "body").getTitle());
        assertEquals(1, service.list().size());
    }

    @Test(expected = IllegalArgumentException.class)
    public void postRequiresTitle() {
        new NoticeService(new NoticeDao()).post(" ", "body");
    }
}
