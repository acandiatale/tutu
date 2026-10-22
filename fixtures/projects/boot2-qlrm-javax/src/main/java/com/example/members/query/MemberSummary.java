package com.example.members.query;

/** Constructor-mapped projection for QLRM (columns by position). */
public class MemberSummary {

    private final Long id;
    private final String email;

    public MemberSummary(Long id, String email) {
        this.id = id;
        this.email = email;
    }

    public Long getId() {
        return id;
    }

    public String getEmail() {
        return email;
    }
}
