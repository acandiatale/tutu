package com.example.shop.notice;

/**
 * 공지사항 문구를 만든다 (MS949 로 저장된 소스).
 */
public class NoticeFormatter {

    public String format(String title) {
        // 제목 앞에 머리말을 붙인다
        return NoticeMessages.PREFIX + " " + title;
    }
}
