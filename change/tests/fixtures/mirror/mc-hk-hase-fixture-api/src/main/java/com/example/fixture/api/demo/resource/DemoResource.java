package com.example.fixture.api.demo.resource;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/demo")
public class DemoResource {
    @GetMapping("/ping")
    public String ping() {
        return "pong";
    }
}

