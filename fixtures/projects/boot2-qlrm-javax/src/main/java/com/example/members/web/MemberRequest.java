package com.example.members.web;

import javax.validation.constraints.Email;
import javax.validation.constraints.NotBlank;
import javax.validation.constraints.Size;

import lombok.Data;

@Data
public class MemberRequest {

    @NotBlank
    @Email
    private String email;

    @NotBlank
    @Size(max = 50)
    private String name;
}
