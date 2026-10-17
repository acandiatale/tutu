package com.example.board.service;

import java.io.File;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.List;

import org.apache.commons.io.FileUtils;

import com.example.board.dao.NoticeDao;
import com.example.board.model.Notice;

public class NoticeService {

    private static final NoticeService INSTANCE = new NoticeService(new NoticeDao());

    private final NoticeDao dao;

    NoticeService(NoticeDao dao) {
        this.dao = dao;
    }

    public static NoticeService getInstance() {
        return INSTANCE;
    }

    public Notice post(String title, String body) {
        if (title == null || title.trim().isEmpty()) {
            throw new IllegalArgumentException("title required");
        }
        return dao.insert(title.trim(), body);
    }

    public List<Notice> list() {
        return dao.findAll();
    }

    public void archive(File dir) throws IOException {
        StringBuilder sb = new StringBuilder();
        for (Notice n : dao.findAll()) {
            sb.append(n.getId()).append('\t').append(n.getTitle()).append('\n');
        }
        FileUtils.writeStringToFile(new File(dir, "notices.tsv"), sb.toString(), StandardCharsets.UTF_8);
    }
}
