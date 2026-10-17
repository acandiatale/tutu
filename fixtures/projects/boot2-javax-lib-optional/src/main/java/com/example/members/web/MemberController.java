package com.example.members.web;

import java.util.List;

import javax.servlet.http.HttpServletRequest;
import javax.validation.Valid;

import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;

import com.company.legacy.web.LegacyRequestHelper;
import com.example.members.domain.Member;
import com.example.members.service.MemberService;

import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;

@Slf4j
@RestController
@RequestMapping("/api/members")
@RequiredArgsConstructor
public class MemberController {

    private final MemberService memberService;

    @GetMapping
    public List<Member> list() {
        return memberService.list();
    }

    @PostMapping
    @ResponseStatus(HttpStatus.CREATED)
    public Member join(@Valid @RequestBody MemberRequest request, HttpServletRequest http) {
        LegacyRequestHelper helper = new LegacyRequestHelper(http);
        log.info("join {} from {} (ajax={})", helper.requestKey(), LegacyRequestHelper.clientIp(http), helper.isAjax());
        return memberService.join(request);
    }

    @DeleteMapping("/{id}")
    @ResponseStatus(HttpStatus.NO_CONTENT)
    public void remove(@PathVariable long id) {
        memberService.remove(id);
    }

    @ExceptionHandler(MemberService.DuplicateMemberException.class)
    @ResponseStatus(HttpStatus.CONFLICT)
    public String duplicate(MemberService.DuplicateMemberException e) {
        return e.getMessage();
    }
}
