package com.example.board.dao;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicLong;

import com.example.board.model.Notice;

/**
 * In-memory DAO (the production version used plain JDBC against Oracle).
 */
public class NoticeDao {

    private final Map<Long, Notice> store = new ConcurrentHashMap<Long, Notice>();
    private final AtomicLong seq = new AtomicLong();

    public Notice insert(String title, String body) {
        Notice n = new Notice(seq.incrementAndGet(), title, body);
        store.put(n.getId(), n);
        return n;
    }

    public Notice findById(long id) {
        return store.get(id);
    }

    public List<Notice> findAll() {
        return new ArrayList<Notice>(store.values());
    }
}
