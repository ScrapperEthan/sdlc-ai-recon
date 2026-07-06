package com.example.fixture.api.other.resource;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/other")
public class OtherResource {
    @GetMapping("/ping")
    public String ping() {
        return "other";
    }
}
