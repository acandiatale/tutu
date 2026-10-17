package com.example.greeting.notice;

import java.text.SimpleDateFormat;
import java.util.Date;

/** 공지사항 포맷터: "[긴급 공지] 2024년 01월 02일 - 제목" */
public class NoticeFormatter {

    /** 공지 등급 - 레거시 코드답게 enum 상수가 한글 식별자입니다. */
    public enum 등급 {
        일반, 긴급
    }

    public String format(등급 level, Date date, String title) {
        // 날짜 형식은 한국어 표기를 따른다
        SimpleDateFormat fmt = new SimpleDateFormat("yyyy년 MM월 dd일");
        String prefix = level == 등급.긴급 ? "[긴급 공지] " : "[공지] ";
        return prefix + fmt.format(date) + " - " + title;
    }
}
