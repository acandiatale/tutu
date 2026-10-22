package com.example.members.web;

import static org.mockito.ArgumentMatchers.any;
import static org.mockito.BDDMockito.given;
import static org.springframework.security.test.web.servlet.request.SecurityMockMvcRequestPostProcessors.httpBasic;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import java.util.Collections;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.mock.mockito.MockBean;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;

import com.example.members.domain.Member;
import com.example.members.service.MemberService;

@SpringBootTest
@AutoConfigureMockMvc
class MemberControllerTest {

    @Autowired
    private MockMvc mockMvc;

    @MockBean
    private MemberService memberService;

    @Test
    void listRequiresAuth() throws Exception {
        mockMvc.perform(get("/api/members")).andExpect(status().isUnauthorized());
    }

    @Test
    void listMembers() throws Exception {
        given(memberService.list()).willReturn(Collections.singletonList(
                Member.builder().id(1L).email("a@example.com").name("A").build()));
        mockMvc.perform(get("/api/members").with(httpBasic("user", "user")))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$[0].email").value("a@example.com"));
    }

    @Test
    void joinValidates() throws Exception {
        given(memberService.join(any())).willReturn(Member.builder().id(2L).email("b@example.com").name("B").build());
        mockMvc.perform(post("/api/members").with(httpBasic("user", "user"))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"email\":\"not-an-email\",\"name\":\"B\"}"))
                .andExpect(status().isBadRequest());
    }
}
